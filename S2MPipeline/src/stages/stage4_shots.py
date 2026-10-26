"""Stage 4 - per-clip shot & camera. Lens and focus are set by code.

Reads output/3_scenes.json (which carries project, characters,
wardrobe_states, props, and per-scene environment). Writes
output/4_scenes-shots.json with the same tree plus a `shot` per clip.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.prompts import ChatPromptTemplate

from models import Shot
from templates import SHOT_LENS, SHOTS_SYSTEM, SHOTS_USER, VOCABULARIES
from .base import PipelineStage, sig


def _done_fn(clip: dict) -> bool:
    return bool((clip.get("shot") or {}).get("shot_type"))


class Stage4Shots(PipelineStage):
    def __init__(self, llm, storage, limit: int | None = None):
        super().__init__(llm, storage)
        self._limit = limit

    @staticmethod
    def _input_sig(scene: dict, clip: dict) -> str:
        return sig({
            "a": clip.get("story_action"),
            "p": clip.get("character_present"),
            "e": (scene.get("environment") or {}).get("name"),
            "t": scene.get("time_of_day"),
        })

    def run(self) -> None:
        st = self._storage
        if not st.scenes_exists():
            print("Error: Missing output/3_scenes.json. "
                  "Run 'scenes' first.")
            return

        clipped = st.read_scenes()

        if st.scenes_shots_exists():
            kept, stale = self.merge_prior(
                clipped, st.read_scenes_shots(), "shot", "shots_sig",
                self._input_sig)
            print(f"Resuming from {st._config.scenes_shots_path} "
                  f"({kept} kept, {stale} stale reset)")

        clips = self._clips_for_stage(clipped, done_fn=_done_fn)
        selected = self._select(clips, "4 (Shots)", self._limit)
        if selected is None:
            print("Cancelled.")
            return

        pending = [(n, sc, c) for n, sc, c, _ in clips if n in selected]
        if not pending:
            print("Nothing to do.")
            st.write_scenes_shots(clipped)
            return

        llm = self._llm.structured(Shot,
                                    temperature=self._llm._config.shots_temperature)
        chain = ChatPromptTemplate.from_messages([
            ("system", SHOTS_SYSTEM), ("human", SHOTS_USER),
        ]) | llm

        def _proc(item):
            num, scene, clip = item
            t0 = time.perf_counter()
            place_name = (scene.get("environment") or {}).get("name", "")
            result: Shot = self._invoke_with_retry(chain, {
                "clip_name":            clip["name"],
                "story_action":         clip["story_action"],
                "place_name":           place_name,
                "time_of_day":          scene["time_of_day"].replace("_", " "),
                "person":               "yes" if clip.get("character_present") else "no",
                "shot_type_list":       ", ".join(VOCABULARIES["shot_type"]),
                "camera_angle_list":    ", ".join(VOCABULARIES["camera_angle"]),
                "camera_movement_list": ", ".join(VOCABULARIES["camera_movement"]),
            }, prompts=(SHOTS_SYSTEM, SHOTS_USER))
            shot = result.model_dump()
            shot["lens_mm"] = SHOT_LENS[shot["shot_type"]]
            shot["focus_point"] = ("the character" if clip.get("character_present")
                                   else "the scene")
            return num, scene, clip, shot, time.perf_counter() - t0

        def _store(res):
            num, scene, clip, shot, elapsed = res
            clip["shot"] = shot
            clip.setdefault("meta", {})["shots_sig"] = self._input_sig(scene, clip)
            st.write_scenes_shots(clipped)
            print(f"[{num}] done ({elapsed:.1f}s) - {clip['clip_id']}")

        processed = failed = 0
        workers = max(1, self._llm._config.max_concurrency)
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

        st.write_scenes_shots(clipped)
        print("\n" + "=" * 60)
        print(f"Stage 4 done. Processed: {processed}  Failed: {failed}")
        print(f"File: {st._config.scenes_shots_path}")