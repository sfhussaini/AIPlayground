"""Stage 6 - per-clip LTX motion & audio -> 6_scenes-motion.json.

Reads output/5_scenes-keyframes.json (already a full tree with project,
characters, wardrobe_states, props, and per-scene environment). Writes the
same tree plus `clip.video`.
"""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.prompts import ChatPromptTemplate

from models import VideoLLM
from templates import (
    DEPTH_LABELS, FACING_LABELS, HORIZONTAL_LABELS, MOTION_SYSTEM,
    MOTION_USER, POSES,
)
from .base import PipelineStage, iter_clips, sig

_LABELS = {p["pose_id"]: p["label"] for p in POSES}
_LABELS["pose_none"] = "nobody"


def _seed(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")


def _done_fn(clip: dict) -> bool:
    m = (clip.get("video") or {}).get("motion") or {}
    return bool(m.get("ambient_motion"))


def _scene_ambience(scene: dict) -> str:
    return (scene.get("environment") or {}).get("ambience", "")


class Stage6Motion(PipelineStage):
    def __init__(self, llm, storage, limit: int | None = None):
        super().__init__(llm, storage)
        self._limit = limit

    def run(self) -> None:
        st, cfg = self._storage, self._llm._config
        if not st.scenes_keyframes_exists():
            print("Error: Missing output/5_scenes-keyframes.json.")
            return

        clipped = st.read_scenes_keyframes()
        for key in ("project", "characters", "wardrobe_states", "props", "scenes"):
            if key not in clipped:
                print(f"Error: 5_scenes-keyframes.json missing '{key}'. "
                      f"Re-run 'keyframes'.")
                return

        states = self._state_lookup(clipped)

        if st.motion_exists():
            kept, stale = self.merge_prior(
                clipped, st.read_motion(), "video", "video_sig",
                lambda sc, c: self._input_sig(sc, c, states))
            print(f"Resuming from {cfg.motion_path} "
                  f"({kept} kept, {stale} stale reset)")

        clips = self._clips_for_stage(clipped, done_fn=_done_fn)
        selected = self._select(clips, "6 (Motion)", self._limit)
        if selected is None:
            print("Cancelled.")
            return
        pending = [(n, sc, c) for n, sc, c, _ in clips if n in selected]
        if not pending:
            print("Nothing to do.")
            st.write_motion(clipped)
            return

        llm = self._llm.structured(VideoLLM, temperature=cfg.motion_temperature)
        chain = ChatPromptTemplate.from_messages([
            ("system", MOTION_SYSTEM), ("human", MOTION_USER),
        ]) | llm

        def _proc(item):
            num, scene, clip = item
            kf = clip.get("keyframes")
            if not kf:
                raise RuntimeError("no keyframes yet - run 'keyframes' first")
            t0 = time.perf_counter()
            present = bool(clip.get("character_present"))
            ambience = _scene_ambience(scene)
            result: VideoLLM = self._invoke_with_retry(chain, {
                "story_action":   clip["story_action"],
                "shot_summary":   self._shot_summary(clip),
                "person":         "yes" if present else "no",
                "first_summary":  self._summary(self._resolve(kf["first"], states)),
                "last_summary":   self._summary(self._resolve(kf["last"], states)),
                "scene_ambience": ambience,
            }, prompts=(MOTION_SYSTEM, MOTION_USER))

            cid = clip["clip_id"]
            video = {
                "task": "ltx_fml", "template_id": "ltx_motion",
                "inputs": {
                    "first":  f"keyframe:{kf['first'].get('reuse_from') or kf['first']['keyframe_id']}",
                    "middle": f"keyframe:{kf['middle']['keyframe_id']}",
                    "last":   f"keyframe:{kf['last']['keyframe_id']}",
                },
                "motion": {
                    "subject_motion":   result.subject_motion.strip() if present else "",
                    "secondary_motion": "",
                    "ambient_motion":   result.ambient_motion.strip(),
                },
                "audio": {"ambience": ambience, "sfx": "", "dialogue": None},
                "negative_prompt_extra": [],
                "seed": _seed(f"{cid}.ltx"),
                "rendered_prompt": None, "negative_prompt": None,
                "status": "pending", "output_path": None,
            }
            return num, scene, clip, video, time.perf_counter() - t0

        def _store(res):
            num, scene, clip, video, elapsed = res
            clip["video"] = video
            clip.setdefault("meta", {})["video_sig"] = self._input_sig(
                scene, clip, states)
            st.write_motion(clipped)
            print(f"[{num}] done ({elapsed:.1f}s) - {clip['clip_id']}")

        processed = failed = 0
        workers = max(1, cfg.max_concurrency)
        if workers == 1:
            for item in pending:
                try:
                    _store(_proc(item))
                    processed += 1
                except Exception as exc:
                    failed += 1
                    print(f"[{item[0]}] error: {exc}")
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_proc, it): it for it in pending}
                for fut in as_completed(futures):
                    try:
                        _store(fut.result())
                        processed += 1
                    except Exception as exc:
                        failed += 1
                        print(f"[{futures[fut][0]}] error: {exc}")

        st.write_motion(clipped)
        print("\n" + "=" * 60)
        print(f"Stage 6 done. Processed: {processed}  Failed: {failed}")
        print(f"File: {cfg.motion_path}")

    # --- helpers --------------------------------------------------------
    @staticmethod
    def _state_lookup(scenes: dict) -> dict:
        out = {}
        for _, clip in iter_clips(scenes):
            for kf in (clip.get("keyframes") or {}).values():
                if kf.get("state"):
                    out[kf["keyframe_id"]] = kf["state"]
        return out

    @staticmethod
    def _resolve(kf: dict, states: dict) -> dict:
        if kf.get("state"):
            return kf["state"]
        return states.get(kf.get("reuse_from") or "", {})

    @staticmethod
    def _input_sig(scene: dict, clip: dict, states: dict) -> str:
        kf = clip.get("keyframes") or {}
        resolved = {r: Stage6Motion._resolve(kf[r], states)
                    for r in kf if isinstance(kf[r], dict)}
        return sig({
            "s": resolved,
            "shot": clip.get("shot"),
            "a": clip.get("story_action"),
            "amb": _scene_ambience(scene),
            "p": clip.get("character_present"),
        })

    @staticmethod
    def _shot_summary(clip: dict) -> str:
        s = clip.get("shot") or {}
        return (f"{s.get('shot_type', '').replace('_', ' ')}, "
                f"{s.get('camera_movement', '').replace('_', ' ')}")

    @staticmethod
    def _summary(s: dict) -> str:
        if not s:
            return "(not set)"
        parts = [_LABELS.get(s.get("pose_id", ""), ""),
                 HORIZONTAL_LABELS.get(s.get("screen_horizontal", ""), ""),
                 FACING_LABELS.get(s.get("facing", ""), ""),
                 s.get("expression", "")]
        return ", ".join(p for p in parts if p)