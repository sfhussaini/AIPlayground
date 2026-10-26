"""Stage 5 - per-clip first/middle/last keyframe states.

Reads output/4_scenes-shots.json (which carries 3_scenes fields + shots).
Character data comes from 4_scenes-shots.json's `characters` block.
Per-story libraries come from `wardrobe_states` and `props` blocks.
Pose library comes from reference/poses.json, with a 'pose_new' escape hatch.

Pipeline:
  1. For each clip, LLM picks pose_id, pose_new_label/text (only if pose_id
     == 'pose_new'), wardrobe_state_id, prop_id, screen_horizontal, facing,
     expression.
  2. Code sets screen_depth='midground' and character_id.
  3. If pose_id=='pose_new': store the proposal on the keyframe, use a
     fallback pose_id (nearest existing, or 'pose_stand') so the file always
     validates. The proposal is reviewed once at the end.
  4. After all clips: batch review pose proposals [a]/[r]/[s]/[q].
     - Accept -> append to reference/poses.json, keyframe.pose_id updated.
     - Reject -> keep fallback pose_id, drop proposal.
  5. Write final file.

Reuse semantics unchanged: first can reference the previous clip's last
if wardrobe_state_id matches. If it differs, reuse is broken and both first
and last are regenerated.

Wardrobe-state consistency: first and last MUST share wardrobe_state_id.
Prop may change. Pose may change. Side/facing/expression may change.
"""
import copy
import hashlib
import re
import sys
import time
from difflib import SequenceMatcher
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate

from models import make_keyframe_models
from reference import POSES, append_pose
from templates import (
    KEYFRAMES_END_SYSTEM, KEYFRAMES_SYSTEM, KEYFRAMES_USER, VOCABULARIES,
)
from .base import PipelineStage, iter_clips, sig


_FALLBACK_POSE = "pose_stand"


def _seed(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")


def _done_fn(clip: dict) -> bool:
    state = (((clip.get("keyframes") or {}).get("last") or {}).get("state") or {})
    return bool(state.get("pose_id"))


def neutral_state(character_id: str) -> dict:
    """Neutral baseline for non-person frames. pose_id='pose_none' is a
    built-in reserved entry (in poses.json's 'no_pose'). wardrobe_state_id
    'default' and prop_id 'none' are the Stage 3b sentinels."""
    return {
        "character_id": character_id,
        "pose_id": "pose_none",
        "pose_new_label": None,
        "pose_new_text": None,
        "wardrobe_state_id": "default",
        "prop_id": "none",
        "screen_horizontal": "center",
        "screen_depth": "midground",
        "facing": "toward_camera",
        "expression": "neutral",
    }


def to_state(llm_state, character_id: str, fallback_pose_id: str) -> dict:
    """Copy LLM output into a state dict.

    Handles the pose escape hatch:
      - If pose_id == 'pose_new': store the proposal, use fallback_pose_id
        as the concrete pose_id so the file always validates. Resolved later.
      - Otherwise: copy pose_id verbatim.

    `reasoning` is intentionally not persisted.
    """
    d = llm_state.model_dump()
    s = neutral_state(character_id)

    pose_id = d["pose_id"]
    if pose_id == "pose_new":
        s["pose_id"] = fallback_pose_id
        s["pose_new_label"] = (d.get("pose_new_label") or "").strip() or None
        s["pose_new_text"] = (d.get("pose_new_text") or "").strip() or None
    else:
        s["pose_id"] = pose_id

    s["wardrobe_state_id"] = d["wardrobe_state_id"]
    s["prop_id"] = d["prop_id"]
    s["screen_horizontal"] = d["screen_horizontal"]
    s["facing"] = d["facing"]
    s["expression"] = d["expression"]
    return s


def compute_middle(first: dict, last: dict) -> dict:
    """Middle frame: one category moves toward the end state."""
    mid = copy.deepcopy(first)
    if (first["pose_id"] != last["pose_id"]
            or first["prop_id"] != last["prop_id"]):
        mid["pose_id"] = last["pose_id"]
        mid["prop_id"] = last["prop_id"]
    elif first["screen_horizontal"] != last["screen_horizontal"]:
        order = VOCABULARIES["screen_horizontal"]
        i, j = (order.index(first["screen_horizontal"]),
                order.index(last["screen_horizontal"]))
        mid["screen_horizontal"] = order[(i + j + 1) // 2 if j > i
                                         else (i + j) // 2]
    elif first["facing"] != last["facing"]:
        mid["facing"] = last["facing"]
    elif first["expression"] != last["expression"]:
        mid["expression"] = last["expression"]
    return mid


def compute_reuse_map(scenes_data: dict) -> dict:
    """clip_id -> previous clip's last keyframe id, same scene, same presence."""
    result, prev = {}, None
    for scene in scenes_data.get("scenes", []):
        sid = scene["scene_id"]
        for clip in scene.get("clips", []):
            cid = clip["clip_id"]
            present = bool(clip.get("character_present"))
            cont = ((clip.get("continuity") or {}).get("transition_in")
                    == "continuous_from_previous")
            if cont and prev and prev[0] == sid and prev[2] == present:
                result[cid] = f"{prev[1]}.last"
            else:
                result[cid] = None
            prev = (sid, cid, present)
    return result


# ---------------------------------------------------------------------------
# Pose helpers (used by deferred review; self-contained here)
# ---------------------------------------------------------------------------
def _pose_slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "action"


def _unique_pose_id(base: str, existing_ids: set[str]) -> str:
    if base not in existing_ids:
        return base
    n = 2
    while f"{base}_{n}" in existing_ids:
        n += 1
    return f"{base}_{n}"


def _find_nearest_pose(label: str, poses: list[dict],
                       threshold: float = 0.5) -> Optional[dict]:
    label_l = label.lower().strip()
    best, best_score = None, threshold
    for p in poses:
        score = SequenceMatcher(None, label_l, p["label"].lower()).ratio()
        if score >= best_score:
            best, best_score = p, score
    return best


def _append_pose_from_proposal(label: str, text: str, cfg) -> str:
    base_id = "pose_" + _pose_slug(label)
    existing_ids = {p["pose_id"] for p in POSES}
    pose_id = _unique_pose_id(base_id, existing_ids)
    pose = {
        "pose_id": pose_id,
        "label": label.strip(),
        "text": text.strip(),
        "source": "keyframe",
    }
    append_pose(pose, persist=cfg.persist_new_poses)
    return pose_id


def _review_pose_proposals(proposals: list[dict], cfg) -> dict[str, str]:
    """Batch-review pose proposals. Returns {label_key: final_pose_id}."""
    if not cfg.interactive_pose_review or not sys.stdin.isatty():
        return {}

    n = len(proposals)
    print(f"\n[{n}] new pose(s) proposed during keyframe generation:")
    for i, p in enumerate(proposals, start=1):
        print(f"  [{i:>2}] {p['label']}: {p['text'][:70]}")

    while True:
        try:
            raw = input(
                "\n  [a] accept all  [r] review one by one  "
                "[s] skip all  [q] quit > "
            ).strip().lower()
        except EOFError:
            return {}

        if raw in ("", "a", "accept", "all"):
            return _accept_all_proposals(proposals, cfg)
        if raw in ("s", "skip", "skip all", "q", "quit"):
            return {}
        if raw in ("r", "review"):
            return _review_proposals_one_by_one(proposals, cfg)
        print("  Unknown choice. Use a, r, s, or q.")


def _accept_all_proposals(proposals: list[dict], cfg) -> dict[str, str]:
    result: dict[str, str] = {}
    for p in proposals:
        try:
            pose_id = _append_pose_from_proposal(p["label"], p["text"], cfg)
            result[p["label_key"]] = pose_id
            print(f"    + {pose_id}")
        except Exception as exc:
            print(f"    x {p['label']}: {exc}")
    return result


def _review_proposals_one_by_one(proposals: list[dict], cfg) -> dict[str, str]:
    result: dict[str, str] = {}
    skip_remaining = False
    for p in proposals:
        if skip_remaining:
            continue
        print(f"\n  pose: {p['label']}")
        print(f"  text: {p['text']}")
        print("  [Enter] accept / [e] edit / [s] skip / [q] skip all remaining")
        while True:
            try:
                cmd = input("  > ").strip().lower()
            except EOFError:
                cmd = "s"
            if cmd in ("", "a", "accept"):
                try:
                    pose_id = _append_pose_from_proposal(
                        p["label"], p["text"], cfg)
                    result[p["label_key"]] = pose_id
                    print(f"    + {pose_id}")
                except Exception as exc:
                    print(f"    x {exc}")
                break
            if cmd in ("s", "skip"):
                break
            if cmd in ("q", "quit"):
                skip_remaining = True
                break
            if cmd in ("e", "edit"):
                new_label = input(f"    label [{p['label']}]: ").strip()
                if new_label:
                    p["label"] = new_label
                new_text = input(f"    text [{p['text']}]: ").strip()
                if new_text:
                    p["text"] = new_text
                print(f"    label: {p['label']!r}")
                print(f"    text: {p['text']!r}")
                continue
            print("  Unknown choice. Use Enter, e, s, or q.")
    return result


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------
class Stage5Keyframes(PipelineStage):
    def __init__(self, llm, storage, limit: int | None = None):
        super().__init__(llm, storage)
        self._limit = limit

    @staticmethod
    def _input_sig(scene, clip, reuse_map, wardrobe_state_ids):
        return sig({
            "shot":      clip.get("shot"),
            "action":    clip.get("story_action"),
            "present":   clip.get("character_present"),
            "wardrobe":  clip.get("wardrobe_id"),
            "state_ids": sorted(wardrobe_state_ids),
            "reuse":     reuse_map.get(clip["clip_id"]),
            "light":     scene.get("light"),
            "chars":     scene.get("characters_present"),
        })

    def run(self) -> None:
        st, cfg = self._storage, self._llm._config
        if not st.scenes_shots_exists():
            print("Error: Missing output/4_scenes-shots.json. "
                  "Run 'shots' first.")
            return
        if cfg.max_concurrency > 1:
            print("Note: stage 5 runs one clip at a time (clips chain).")

        clipped = st.read_scenes_shots()
        for key in ("characters", "wardrobe_states", "props", "scenes"):
            if key not in clipped:
                print(f"Error: 4_scenes-shots.json missing '{key}'. "
                      f"Re-run 'scenes' and 'shots'.")
                return

        reuse_map = compute_reuse_map(clipped)

        chars = clipped.get("characters") or []
        if not chars:
            print("Error: no character in 4_scenes-shots.json.")
            return
        character = chars[0]
        if not character.get("wardrobe"):
            print("Error: character has no wardrobe.")
            return

        wardrobe_states_list = clipped["wardrobe_states"]
        wardrobe_state_ids = [s["state_id"] for s in wardrobe_states_list]
        wardrobe_text_by_id = {s["state_id"]: s["text"]
                                for s in wardrobe_states_list}

        props_list = clipped["props"]
        prop_ids = [p["prop_id"] for p in props_list]
        prop_text_by_id = {p["prop_id"]: p["text"] for p in props_list}

        if "default" not in wardrobe_state_ids:
            print("Error: wardrobe_states missing 'default'. Re-run 'scenes'.")
            return
        if "none" not in prop_ids:
            print("Error: props missing 'none'. Re-run 'scenes'.")
            return

        print(f"  wardrobe_state enum: {wardrobe_state_ids}")
        print(f"  prop enum: {prop_ids}")

        if st.scenes_keyframes_exists():
            kept, stale = self.merge_prior(
                clipped, st.read_scenes_keyframes(), "keyframes", "keyframes_sig",
                lambda sc, c: self._input_sig(sc, c, reuse_map,
                                              wardrobe_state_ids))
            print(f"Resuming from {cfg.scenes_keyframes_path} "
                  f"({kept} kept, {stale} stale reset)")
        self._invalidate_dependents(clipped, reuse_map, None)

        clips = self._clips_for_stage(clipped, done_fn=_done_fn)
        selected = self._select(clips, "5 (Keyframes)", self._limit)
        if selected is None:
            print("Cancelled.")
            return

        pending = [(n, sc, c) for n, sc, c, _ in clips if n in selected]

        pose_ids = [p["pose_id"] for p in POSES]
        Full, End = make_keyframe_models(pose_ids, wardrobe_state_ids,
                                          prop_ids)
        chain_full = ChatPromptTemplate.from_messages(
            [("system", KEYFRAMES_SYSTEM), ("human", KEYFRAMES_USER)]
        ) | self._llm.structured(Full, temperature=cfg.keyframes_temperature)
        chain_end = ChatPromptTemplate.from_messages(
            [("system", KEYFRAMES_END_SYSTEM), ("human", KEYFRAMES_USER)]
        ) | self._llm.structured(End, temperature=cfg.keyframes_temperature)

        names = {p["pose_id"]: p["label"] for p in POSES}
        wardrobe_states_prompt = "\n".join(
            f"  {sid}: {text or '(unchanged - see wardrobe)'}"
            for sid, text in wardrobe_text_by_id.items())
        props_prompt = "\n".join(
            f"  {pid}: {text or '(nothing)'}"
            for pid, text in prop_text_by_id.items())

        ctx = {
            "reuse_map": reuse_map,
            "clip_by_id": {c["clip_id"]: c for _, c in iter_clips(clipped)},
            "chain_full": chain_full, "chain_end": chain_end,
            "poses_list": "\n".join(f"  {pid}: {lab}"
                                     for pid, lab in names.items()),
            "wardrobe_states_list": wardrobe_states_prompt,
            "props_list": props_prompt,
            "wardrobe_state_ids": wardrobe_state_ids,
            "prop_ids": prop_ids,
            "wardrobe_text_by_id": wardrobe_text_by_id,
            "prop_text_by_id": prop_text_by_id,
            "names": names,
            "fallback_pose": _FALLBACK_POSE,
        }

        processed = failed = 0
        for num, scene, clip in pending:
            try:
                elapsed = self._process(scene, clip, ctx)
                st.write_scenes_keyframes(clipped)
                self._invalidate_dependents(clipped, reuse_map, clip["clip_id"])
                st.write_scenes_keyframes(clipped)
                processed += 1
                print(f"[{num}] done ({elapsed:.1f}s) - {clip['clip_id']}")
            except Exception as exc:
                failed += 1
                print(f"[{num}] error: {exc}")

        # Batch-review any pose_new proposals (also runs when pending was empty,
        # to resolve leftover proposals from a previous run).
        self._resolve_pose_proposals(clipped, cfg)

        st.write_scenes_keyframes(clipped)
        print("\n" + "=" * 60)
        print(f"Stage 5 done. Processed: {processed}  Failed: {failed}")
        print(f"File: {cfg.scenes_keyframes_path}")

    # ------------------------------------------------------------------
    def _resolve_pose_proposals(self, clipped: dict, cfg) -> None:
        """Collect unresolved pose_new proposals, batch-review, apply."""
        proposals_by_label: dict[str, list[dict]] = {}
        for _, clip in iter_clips(clipped):
            kfs = clip.get("keyframes") or {}
            for role in ("first", "middle", "last"):
                st = (kfs.get(role) or {}).get("state") or {}
                label = st.get("pose_new_label")
                text = st.get("pose_new_text")
                if not label or not text:
                    continue
                lk = label.lower().strip()
                proposals_by_label.setdefault(lk, []).append(st)

        if not proposals_by_label:
            return

        proposals = [
            {"label_key": k, "label": v[0]["pose_new_label"],
             "text": v[0]["pose_new_text"]}
            for k, v in proposals_by_label.items()
        ]

        # Dedupe against existing POSES: if near-match, auto-resolve
        unresolved = []
        for p in proposals:
            near = _find_nearest_pose(p["label"], POSES, threshold=0.75)
            if near:
                for st in proposals_by_label[p["label_key"]]:
                    st["pose_id"] = near["pose_id"]
                    st["pose_new_label"] = None
                    st["pose_new_text"] = None
                print(f"  pose '{p['label']}' deduped to {near['pose_id']}")
            else:
                unresolved.append(p)

        if not unresolved:
            return

        accepted_map = _review_pose_proposals(unresolved, cfg)

        for p in unresolved:
            lk = p["label_key"]
            if lk in accepted_map:
                for st in proposals_by_label[lk]:
                    st["pose_id"] = accepted_map[lk]
                    st["pose_new_label"] = None
                    st["pose_new_text"] = None
            else:
                near = _find_nearest_pose(p["label"], POSES, threshold=0.4)
                fallback = near["pose_id"] if near else _FALLBACK_POSE
                for st in proposals_by_label[lk]:
                    st["pose_id"] = fallback
                    st["pose_new_label"] = None
                    st["pose_new_text"] = None

    # ------------------------------------------------------------------
    def _process(self, scene: dict, clip: dict, ctx: dict) -> float:
        clip_id = clip["clip_id"]
        if not clip.get("shot"):
            raise RuntimeError("no shot yet - run 'shots' first")

        chars = scene.get("characters_present") or []
        present = bool(clip.get("character_present")) and bool(chars)
        character_id = chars[0] if present else ""
        if present and len(chars) > 1:
            print(f"  warning: multiple characters in {scene['scene_id']}; "
                  f"using {character_id}")

        reuse_from = ctx["reuse_map"].get(clip_id)
        prev_last = None
        if reuse_from:
            prev = ctx["clip_by_id"].get(reuse_from.rsplit(".", 1)[0]) or {}
            prev_last = (((prev.get("keyframes") or {}).get("last")
                          or {}).get("state"))
            if not prev_last:
                raise RuntimeError(
                    f"previous clip ({reuse_from}) has no keyframes yet - "
                    f"process it first")

        t0 = time.perf_counter()
        reuse_broken = False
        if present:
            inputs = self._inputs(clip, prev_last, ctx)
            fallback_pose = ctx["fallback_pose"]
            if prev_last:
                r = self._invoke_with_retry(
                    ctx["chain_end"], inputs,
                    prompts=(KEYFRAMES_END_SYSTEM, KEYFRAMES_USER))
                last = to_state(r.last, character_id, fallback_pose)
                if last["wardrobe_state_id"] == prev_last["wardrobe_state_id"]:
                    first = copy.deepcopy(prev_last)
                else:
                    print(f"  {clip_id}: wardrobe_state_id "
                          f"{prev_last['wardrobe_state_id']!r} -> "
                          f"{last['wardrobe_state_id']!r}; breaking reuse")
                    r2 = self._invoke_with_retry(
                        ctx["chain_full"], inputs,
                        prompts=(KEYFRAMES_SYSTEM, KEYFRAMES_USER))
                    first = to_state(r2.first, character_id, fallback_pose)
                    last = to_state(r2.last, character_id, fallback_pose)
                    reuse_broken = True
            else:
                r = self._invoke_with_retry(
                    ctx["chain_full"], inputs,
                    prompts=(KEYFRAMES_SYSTEM, KEYFRAMES_USER))
                first = to_state(r.first, character_id, fallback_pose)
                last = to_state(r.last, character_id, fallback_pose)

            if first["wardrobe_state_id"] != last["wardrobe_state_id"]:
                print(f"  warning: {clip_id} first/last wardrobe_state_id "
                      f"mismatch ({first['wardrobe_state_id']!r} vs "
                      f"{last['wardrobe_state_id']!r}); using last")
                first["wardrobe_state_id"] = last["wardrobe_state_id"]
        else:
            first = (copy.deepcopy(prev_last) if prev_last
                     else neutral_state(""))
            last = copy.deepcopy(first)

        mid = compute_middle(first, last)

        template = "qwen_keyframe" if present else "qwen_scene"
        kfs = {}
        for role, state, ts in (("first", first, 0.0),
                                 ("middle", mid, 2.5),
                                 ("last", last, 5.0)):
            kf = {
                "keyframe_id": f"{clip_id}.{role}", "timestamp_sec": ts,
                "reuse_from": None, "delta_from_previous": None,
                "must_preserve_extra": [],
            }
            if role == "first" and reuse_from and not reuse_broken:
                kf["reuse_from"] = reuse_from
            else:
                kf["state"] = state
                kf["image_gen"] = {
                    "task": "qwen_image_edit", "template_id": template,
                    "seed": _seed(f"{clip_id}.{role}.qwen"),
                    "status": "pending", "output_path": None,
                    "rendered_prompt": None,
                }
            kfs[role] = kf
        clip["keyframes"] = kfs
        clip.setdefault("meta", {})["keyframes_sig"] = self._input_sig(
            scene, clip, ctx["reuse_map"], ctx["wardrobe_state_ids"])
        return time.perf_counter() - t0

    def _inputs(self, clip: dict, prev_last, ctx: dict) -> dict:
        shot = clip["shot"]
        return {
            "story_action": clip["story_action"],
            "shot_summary": f"{shot['shot_type'].replace('_', ' ')}, "
                            f"{shot['camera_angle'].replace('_', ' ')}",
            "start_state": (self._state_text(prev_last, ctx) if prev_last
                            else "(you choose the start)"),
            "poses_list":   ctx["poses_list"],
            "props_list":   ctx["props_list"],
            "wardrobe_states_list": ctx["wardrobe_states_list"],
            "screen_horizontal_list": ", ".join(
                VOCABULARIES["screen_horizontal"]),
            "facing_list":            ", ".join(VOCABULARIES["facing"]),
            "expression_list":        ", ".join(VOCABULARIES["expression"]),
        }

    @staticmethod
    def _state_text(s: dict, ctx: dict) -> str:
        pose_label = ctx["names"].get(s["pose_id"], "")
        ws_text = (ctx["wardrobe_text_by_id"].get(s["wardrobe_state_id"], "")
                   or "default (unchanged)")
        prop_text = (ctx["prop_text_by_id"].get(s["prop_id"], "")
                     or "nothing")
        return (f"pose {s['pose_id']} ({pose_label}), "
                f"side {s['screen_horizontal']}, facing {s['facing']}, "
                f"expression {s['expression']}, prop {prop_text}, "
                f"wardrobe {ws_text}")

    @staticmethod
    def _clear(clip: dict) -> None:
        clip.pop("keyframes", None)
        (clip.get("meta") or {}).pop("keyframes_sig", None)

    def _invalidate_dependents(self, scenes: dict, reuse_map: dict,
                                changed_id) -> None:
        dirty = {changed_id} if changed_id else set()
        have = {c["clip_id"] for _, c in iter_clips(scenes)
                if c.get("keyframes")}
        for _, clip in iter_clips(scenes):
            src = reuse_map.get(clip["clip_id"])
            if not src:
                continue
            prev_id = src.rsplit(".", 1)[0]
            if prev_id in dirty or prev_id not in have:
                if clip.get("keyframes"):
                    self._clear(clip)
                dirty.add(clip["clip_id"])
                have.discard(clip["clip_id"])