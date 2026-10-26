"""Stage 2 - assets.

Fragments-first: reads input/story_fragmented.json, not story.txt.

  - Characters from input/char_XX_sheet.json (Vision + human review).
    Wardrobe starts at {"default": ...} (written by Stage 1.5). Stage 2
    proposes extra per-story states via one LLM call per wardrobe, derived
    from clip actions, and the human accepts/rejects via a batch review.
  - Environments: fragments give free-form scene env names. Group by slug,
    one EnvDescriptionLLM call per unique slug.
  - Props: one ScenePropsLLM call per scene (actions -> objects used).
  - Poses: dedup all clip actions, pre-filter against reference/poses.json,
    one BatchPosesLLM call for the remainder using pose_model_name.
    Batch review: [a] accept all / [r] review one by one / [s] skip all / [q] quit.

Environments, props, and wardrobe states are PER-STORY. They live only in
output/2_assets.json. reference/ stays story-independent.
"""
import re
import sys
import time
from difflib import SequenceMatcher
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate

from models import (
    BatchPosesLLM, EnvDescriptionLLM, PoseNeededLLM, ScenePropsLLM,
    StateProposal, StoryStatesLLM,
)
from reference import (
    NO_POSE, POSES, append_pose, validate_new_pose,
)
from templates import (
    ENV_DESCRIPTION_SYSTEM, ENV_DESCRIPTION_USER,
    POSES_SYSTEM, POSES_USER,
    SCENE_PROPS_SYSTEM, SCENE_PROPS_USER,
    STORY_STATES_SYSTEM, STORY_STATES_USER,
)
from validate import compose_identity_lock, load_all_characters, lock_is_valid
from .base import PipelineStage


_DEFAULT_WARDROBE_STATE = "worn as shown in the reference sheet"


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------
_ARTICLES = re.compile(r"^(the|a|an|her|his|their|its)\s+")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "place"


def _env_slug(name: str) -> str:
    """Normalize an environment name to a stable slug. Strips leading
    articles so 'the kitchen' and 'Kitchen' collapse to 'kitchen'."""
    s = _ARTICLES.sub("", name.lower().strip())
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "place"


def _pose_slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "action"


def _unique_pose_id(base: str, existing_ids: set[str]) -> str:
    if base not in existing_ids:
        return base
    n = 2
    while f"{base}_{n}" in existing_ids:
        n += 1
    return f"{base}_{n}"


def _unique_label(base: str, existing_labels: set[str]) -> str:
    existing_lower = {e.lower() for e in existing_labels}
    if base.lower() not in existing_lower:
        return base
    n = 2
    while f"{base} {n}".lower() in existing_lower:
        n += 1
    return f"{base} {n}"


def _word_jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _find_matching_pose(label: str, description: str,
                        poses: list[dict], threshold: float) -> Optional[dict]:
    label_l = label.lower().strip()
    desc_l = description.lower().strip()
    for p in poses:
        if p["label"].lower() == label_l:
            return p
        scores = (
            SequenceMatcher(None, label_l, p["label"].lower()).ratio(),
            SequenceMatcher(None, desc_l, p["text"].lower()).ratio(),
            _word_jaccard(f"{label} {description}",
                          f"{p['label']} {p['text']}"),
        )
        if max(scores) >= threshold:
            return p
    return None


# ---------------------------------------------------------------------------
# Interactive pose review (per-pose helpers, reused by [r] mode)
# ---------------------------------------------------------------------------
def _print_pose(pose: dict) -> None:
    print(f"    pose_id   [{pose['pose_id']}]")
    print(f"    label     [{pose['label']}]")
    print(f"    text      [{pose['text']}]")


def _check_pose_field(field: str, value: str,
                      existing_poses: list[dict]) -> Optional[str]:
    if field == "pose_id":
        if not re.match(r"^pose_[a-z][a-z0-9_]*$", value):
            return "must match ^pose_[a-z][a-z0-9_]*$"
        if value == "pose_none":
            return "'pose_none' is reserved"
        if any(p["pose_id"] == value for p in existing_poses):
            return "already exists"
    elif field == "label":
        v = value.strip()
        if not v or len(v) > 40:
            return "must be 1-40 characters"
    elif field == "text":
        v = value.strip()
        if len(v) < 10 or len(v) > 300:
            return "must be 10-300 characters"
    return None


def _edit_field(name: str, current: str,
                existing_poses: list[dict]) -> str:
    while True:
        try:
            raw = input(f"    {name:9} [{current}]: ").strip()
        except EOFError:
            return current
        if raw == "":
            return current
        err = _check_pose_field(name, raw, existing_poses)
        if err:
            print(f"      x {name} {err}")
            continue
        return raw


def _edit_pose(pose: dict, existing_poses: list[dict]) -> dict:
    pid = _edit_field("pose_id", pose["pose_id"], existing_poses)
    label = _edit_field("label", pose["label"], existing_poses)
    text = _edit_field("text", pose["text"], existing_poses)
    return {**pose, "pose_id": pid, "label": label, "text": text}


def _proposal_to_pose(needed: PoseNeededLLM) -> dict:
    base_id = "pose_" + _pose_slug(needed.label)
    pid = _unique_pose_id(base_id, {p["pose_id"] for p in POSES})
    label = _unique_label(needed.label.strip(), {p["label"] for p in POSES})
    return {
        "pose_id": pid,
        "label": label,
        "text": needed.description.strip(),
        "source": "user",
    }


def _ask_user_for_pose(needed: PoseNeededLLM,
                       existing_poses: list[dict]) -> tuple[str, Optional[dict]]:
    pose = _proposal_to_pose(needed)

    print(f"\n  [stage 2 / poses] New pose needed: \"{needed.label}\"")
    _print_pose(pose)
    print("  [Enter] accept / [e] edit / [s] skip / [q] skip all remaining")

    while True:
        try:
            cmd = input("  > ").strip().lower()
        except EOFError:
            return ("skip", None)

        if cmd in ("", "a", "accept"):
            err = validate_new_pose(pose, existing_poses)
            if err:
                print(f"  ! default invalid: {err}")
                pose = _edit_pose(pose, existing_poses)
                _print_pose(pose)
                continue
            return ("accept", pose)
        if cmd in ("s", "skip"):
            return ("skip", None)
        if cmd in ("q", "quit"):
            return ("quit", None)
        if cmd in ("e", "edit"):
            pose = _edit_pose(pose, existing_poses)
            _print_pose(pose)
            print("  [Enter] accept / [e] edit again / [s] skip / [q] quit")
            continue
        print("  Unknown command. Use Enter, e, s, or q.")


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------
class Stage2Assets(PipelineStage):
    def __init__(self, llm, storage, force: bool = False):
        super().__init__(llm, storage)
        self._force = force

    def run(self) -> None:
        if self._force:
            self._storage.delete_assets()
        if self._storage.assets_exists():
            print("Stage 2 already complete (output/2_assets.json). "
                  "Use --force to regenerate.")
            return
        if not self._storage.fragmented_exists():
            print("Error: missing input/story_fragmented.json. "
                  "Run 'python main.py breakdown' first.")
            return

        entries, issues = load_all_characters(self._storage)
        for lvl, msg in issues:
            print(f"  {lvl}: {msg}")
        if any(lvl == "ERROR" for lvl, _ in issues):
            print("\nFix character file(s) / sheet(s) and re-run this stage.")
            return
        if not entries:
            print("Error: no character found. "
                  "Run 'python main.py characters' to extract from sheets.")
            return
        if len(entries) > 1:
            print(f"Error: {len(entries)} characters found; "
                  f"multi-character is not yet supported.")
            return
        entry = entries[0]

        cfg = self._llm._config
        sheet_path = cfg.input_path / entry["sheet_file"]
        lock = entry["identity_lock"]
        if not lock_is_valid(lock, entry):
            lock = compose_identity_lock(entry)
            print(f"  identity_lock regenerated: {lock}")

        character = {
            "character_id":  entry["character_id"],
            "name":          entry["name"],
            "subject":       entry["subject"],
            "identity":      entry["identity"],
            "identity_lock": lock,
            "wardrobe":      entry["wardrobe"],
            "sheet":         {"path": str(sheet_path), "status": "ready"},
            "sheet_check":   entry["sheet_check"],
            "review":        entry["review"],
        }
        # Stage 1.5 wrote {"default": ...}; keep it as the baseline.
        for w in character.get("wardrobe", []):
            w.setdefault("states", {})
            if "default" not in w["states"]:
                w["states"]["default"] = _DEFAULT_WARDROBE_STATE

        fragments = self._storage.read_fragmented()
        scenes = [sc for seq in fragments.get("sequences", [])
                  for sc in seq.get("scenes", [])]
        if not scenes:
            print("Error: fragments contain no scenes.")
            return

        t0 = time.perf_counter()
        all_actions = self._collect_actions(scenes)

        # ---- 1. Wardrobe states: per wardrobe, from clip actions --------
        print(f"\nProposing wardrobe states per outfit...")
        self._propose_wardrobe_states(character, all_actions, cfg)

        # ---- 2. Environments: group by slug, one call per unique slug ---
        print(f"\nConsolidating environments from {len(scenes)} scene(s)...")
        environments = self._consolidate_environments(scenes)

        # ---- 3. Props: one call per scene --------------------------------
        print(f"\nExtracting props per scene...")
        props = self._extract_props(scenes)

        # ---- 4. Poses: dedup actions, pre-filter, one batch call ---------
        print(f"\nExtracting poses (batch, model={cfg.pose_model_name})...")
        self._extract_poses(all_actions, cfg)

        print(f"\nStage 2 LLM work took {time.perf_counter() - t0:.1f}s")

        if not environments:
            print("Error: no environments could be described.")
            return

        data = {
            "schema_version": "2.0",
            "assets": {
                "characters":   [character],
                "environments": environments,
                "props":        props,
                "pose_library": [dict(p) for p in POSES] + [dict(NO_POSE)],
            },
        }
        self._storage.write_assets(data)
        print(f"\nWrote {cfg.assets_path} ({len(environments)} place(s), "
              f"{len(props)} prop(s), {len(POSES)} poses, "
              f"{sum(len(w['states']) for w in character['wardrobe'])} state(s))")

    # ------------------------------------------------------------------
    # Action collection
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_actions(scenes: list[dict]) -> list[str]:
        seen: set[str] = set()
        actions: list[str] = []
        for sc in scenes:
            for cl in sc.get("clips", []):
                a = (cl.get("action") or "").strip()
                k = a.lower()
                if k and k not in seen:
                    seen.add(k)
                    actions.append(a)
        return actions

    # ------------------------------------------------------------------
    # Wardrobe states
    # ------------------------------------------------------------------
    def _propose_wardrobe_states(self, character: dict,
                                 actions: list[str], cfg) -> None:
        if not cfg.interactive_state_review:
            print(f"  interactive_state_review=False; "
                  f"every wardrobe keeps only 'default'.")
            return
        if not sys.stdin.isatty():
            print(f"  not a TTY; every wardrobe keeps only 'default'.")
            return
        if not actions:
            return

        total_proposed = total_accepted = 0
        for w in character.get("wardrobe", []):
            existing = set(w["states"].keys())
            remaining = 5 - len(existing)   # validator caps at 5 total
            if remaining <= 0:
                continue

            proposals = self._propose_states_for(w, actions, cfg)
            proposals = [p for p in proposals if p.state_id not in existing]
            proposals = proposals[:remaining]
            if not proposals:
                continue

            total_proposed += len(proposals)
            accepted = self._batch_review_states(
                w["wardrobe_id"], proposals, cfg)
            for state_id, text in accepted.items():
                w["states"][state_id] = text
                total_accepted += 1

        if total_proposed:
            print(f"  wardrobe states: {total_accepted}/{total_proposed} accepted")

    def _propose_states_for(self, wardrobe: dict,
                            actions: list[str], cfg) -> list[StateProposal]:
        garments_text = "; ".join(
            f"{g['color']} {g['type']}" for g in wardrobe.get("garments", [])
        ) or "(none)"

        chain = ChatPromptTemplate.from_messages([
            ("system", STORY_STATES_SYSTEM),
            ("human",  STORY_STATES_USER),
        ]) | self._llm.structured(StoryStatesLLM,
                                   temperature=cfg.state_temperature)
        try:
            result: StoryStatesLLM = self._invoke_with_retry(chain, {
                "wardrobe_text": wardrobe.get("text", ""),
                "garments":      garments_text,
                "footwear":      wardrobe.get("footwear", ""),
                "actions_list":  "\n".join(f"- {a}" for a in actions),
            }, prompts=(STORY_STATES_SYSTEM, STORY_STATES_USER))
        except Exception as exc:
            print(f"  states for {wardrobe.get('wardrobe_id')}: "
                  f"error: {type(exc).__name__}: {exc}")
            return []
        return list(result.states)

    def _batch_review_states(self, wardrobe_id: str,
                             proposals: list[StateProposal],
                             cfg) -> dict[str, str]:
        if not cfg.interactive_state_review or not sys.stdin.isatty():
            return {}

        n = len(proposals)
        print(f"\n[{n}] new wardrobe state(s) proposed for {wardrobe_id}:")
        for i, p in enumerate(proposals, start=1):
            print(f"  [{i:>2}] {p.state_id}: {p.text}")

        while True:
            try:
                raw = input(
                    "\n  [a] accept all  [r] review one by one  "
                    "[s] skip all  [q] quit > "
                ).strip().lower()
            except EOFError:
                return {}
            if raw in ("", "a", "accept", "all"):
                return {p.state_id: p.text for p in proposals}
            if raw in ("s", "skip", "skip all"):
                return {}
            if raw in ("q", "quit"):
                return {}
            if raw in ("r", "review"):
                return self._review_states_one_by_one(proposals)
            print("  Unknown choice. Use a, r, s, or q.")

    def _review_states_one_by_one(self,
                                  proposals: list[StateProposal]
                                  ) -> dict[str, str]:
        accepted: dict[str, str] = {}
        skip_remaining = False
        for p in proposals:
            if skip_remaining:
                continue
            print(f"\n  state: {p.state_id} = {p.text!r}")
            print("  [Enter] accept / [e] edit text / [s] skip / [q] skip all remaining")
            while True:
                try:
                    cmd = input("  > ").strip().lower()
                except EOFError:
                    cmd = "s"
                if cmd in ("", "a", "accept"):
                    accepted[p.state_id] = p.text
                    break
                if cmd in ("s", "skip"):
                    break
                if cmd in ("q", "quit"):
                    skip_remaining = True
                    break
                if cmd in ("e", "edit"):
                    try:
                        new_text = input(f"    text [{p.text}]: ").strip()
                    except EOFError:
                        new_text = ""
                    if new_text:
                        p.text = new_text
                    print(f"    new text: {p.text!r}")
                    continue
                print("  Unknown choice. Use Enter, e, s, or q.")
        return accepted

    # ------------------------------------------------------------------
    # Environments
    # ------------------------------------------------------------------
    def _consolidate_environments(self, scenes: list[dict]) -> list[dict]:
        cfg = self._llm._config
        groups: dict[str, dict] = {}
        for sc in scenes:
            raw = (sc.get("environment_name") or "").strip()
            if not raw:
                continue
            slug = _env_slug(raw)
            g = groups.setdefault(slug, {"display_name": raw, "summaries": []})
            g["summaries"].append(sc.get("summary", ""))

        chain = ChatPromptTemplate.from_messages([
            ("system", ENV_DESCRIPTION_SYSTEM),
            ("human",  ENV_DESCRIPTION_USER),
        ]) | self._llm.structured(EnvDescriptionLLM,
                                   temperature=cfg.assets_temperature)

        environments = []
        for slug, g in groups.items():
            try:
                result: EnvDescriptionLLM = self._invoke_with_retry(chain, {
                    "place_name":      g["display_name"],
                    "scene_summaries": "\n".join(f"- {s}" for s in g["summaries"]),
                }, prompts=(ENV_DESCRIPTION_SYSTEM, ENV_DESCRIPTION_USER))
            except Exception as exc:
                print(f"  env '{slug}': error: {type(exc).__name__}: {exc}")
                continue
            environments.append({
                "environment_id": f"env_{slug}",
                "name":           g["display_name"],
                "description":    result.description.strip(),
                "ambience":       result.ambience.strip(),
            })
            print(f"  env_{slug}: {result.description.strip()[:60]}")
        return environments

    # ------------------------------------------------------------------
    # Props
    # ------------------------------------------------------------------
    def _extract_props(self, scenes: list[dict]) -> list[dict]:
        cfg = self._llm._config
        chain = ChatPromptTemplate.from_messages([
            ("system", SCENE_PROPS_SYSTEM),
            ("human",  SCENE_PROPS_USER),
        ]) | self._llm.structured(ScenePropsLLM,
                                   temperature=cfg.assets_temperature)

        props: list[dict] = []
        seen: set[str] = set()
        for sc in scenes:
            actions = [(cl.get("action") or "").strip()
                       for cl in sc.get("clips", [])]
            actions = [a for a in actions if a]
            if not actions:
                continue
            try:
                result: ScenePropsLLM = self._invoke_with_retry(chain, {
                    "actions_list": "\n".join(f"- {a}" for a in actions),
                    "known_props":  ", ".join(p["name"] for p in props) or "(none)",
                }, prompts=(SCENE_PROPS_SYSTEM, SCENE_PROPS_USER))
            except Exception as exc:
                print(f"  props {sc.get('scene_id')}: error: "
                      f"{type(exc).__name__}: {exc}")
                continue
            for p in result.props:
                key = _slug(p.name)
                if key in seen:
                    continue
                seen.add(key)
                props.append({
                    "prop_id": f"prop_{len(props) + 1:02d}",
                    "name":    p.name.strip(),
                    "text":    p.text.strip(),
                })
        return props

    # ------------------------------------------------------------------
    # Poses
    # ------------------------------------------------------------------
    def _extract_poses(self, actions: list[str], cfg) -> None:
        if not actions:
            return

        to_generate = [a for a in actions
                       if _find_matching_pose(a, "", POSES,
                                              cfg.pose_match_threshold) is None]
        if not to_generate:
            print(f"  all {len(actions)} action(s) already covered by pose library")
            return

        print(f"  {len(to_generate)}/{len(actions)} action(s) need new poses; "
              f"calling pose LLM ({cfg.pose_model_name})...")
        chain = ChatPromptTemplate.from_messages([
            ("system", POSES_SYSTEM),
            ("human",  POSES_USER),
        ]) | self._llm.pose_structured(BatchPosesLLM)

        try:
            result: BatchPosesLLM = self._invoke_with_retry(chain, {
                "actions_list": "\n".join(f"- {a}" for a in to_generate),
            }, prompts=(POSES_SYSTEM, POSES_USER))
        except Exception as exc:
            print(f"  poses: error: {type(exc).__name__}: {exc}")
            return

        proposals = [p for p in result.poses
                     if _find_matching_pose(p.label, p.description, POSES,
                                            cfg.pose_match_threshold) is None]
        if not proposals:
            print(f"  pose LLM returned {len(result.poses)} proposal(s), "
                  f"all already covered")
            return

        accepted, skipped = self._batch_review_poses(proposals, cfg)
        if accepted:
            print(f"  + {len(accepted)} new pose(s) appended")
        if skipped:
            print(f"  - {len(skipped)} pose(s) skipped: {', '.join(skipped)}")

    def _batch_review_poses(self, proposals: list[PoseNeededLLM],
                             cfg) -> tuple[list[dict], list[str]]:
        if not cfg.interactive_pose_review or not sys.stdin.isatty():
            return [], [p.label for p in proposals]

        n = len(proposals)
        print(f"\n[{n}] new pose(s) proposed:")
        for i, p in enumerate(proposals, start=1):
            print(f"  [{i:>2}] {p.label}: {p.description[:70]}")

        while True:
            try:
                raw = input(
                    "\n  [a] accept all   [r] review one by one   "
                    "[s] skip all   [q] quit > "
                ).strip().lower()
            except EOFError:
                return [], [p.label for p in proposals]

            if raw in ("", "a", "accept", "all"):
                return self._append_all(proposals, cfg)
            if raw in ("s", "skip", "skip all"):
                return [], [p.label for p in proposals]
            if raw in ("q", "quit"):
                return [], [p.label for p in proposals]
            if raw in ("r", "review"):
                return self._review_poses_one_by_one(proposals, cfg)
            print("  Unknown choice. Use a, r, s, or q.")

    def _append_all(self, proposals: list[PoseNeededLLM],
                    cfg) -> tuple[list[dict], list[str]]:
        accepted, skipped = [], []
        for p in proposals:
            pose = _proposal_to_pose(p)
            try:
                append_pose(pose, persist=cfg.persist_new_poses)
                accepted.append(pose)
                print(f"    + {pose['pose_id']}")
            except Exception as exc:
                print(f"    x {pose['pose_id']}: {exc}")
                skipped.append(p.label)
        return accepted, skipped

    def _review_poses_one_by_one(self, proposals: list[PoseNeededLLM],
                                 cfg) -> tuple[list[dict], list[str]]:
        accepted, skipped = [], []
        skip_remaining = False
        for p in proposals:
            if skip_remaining:
                skipped.append(p.label)
                continue
            action, new_pose = _ask_user_for_pose(p, POSES)
            if action == "accept" and new_pose is not None:
                try:
                    append_pose(new_pose, persist=cfg.persist_new_poses)
                    accepted.append(new_pose)
                    print(f"    + {new_pose['pose_id']}")
                except Exception as exc:
                    print(f"    x {new_pose.get('pose_id', '?')}: {exc}")
                    skipped.append(p.label)
            elif action == "quit":
                skip_remaining = True
                skipped.append(p.label)
            else:
                skipped.append(p.label)
        return accepted, skipped