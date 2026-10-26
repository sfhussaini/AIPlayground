"""Stage 8 - render video.rendered_prompt for every clip. Pure code, no LLM.

Reads 7_qwen-prompts.json (full tree: project, characters, wardrobe_states,
props, scenes). Writes 8_ltx-prompts.json with the same tree plus
video.rendered_prompt and video.negative_prompt filled in.

Only fields consumed:
  - data["project"]["style_bible"]      -> visual_style, video_negative_terms
  - clip["shot"]                        -> shot_type, camera_movement
  - clip["video"]["motion"]             -> subject_motion, secondary_motion,
                                           ambient_motion

No character/env/wardrobe/prop lookups - LTX motion is body + world motion.
Always re-renders everything.
"""
import time

from templates import NEGATIVES_VIDEO, PROMPT_TEMPLATES
from .base import PipelineStage, iter_clips, render_sentences


class Stage8LtxPrompts(PipelineStage):
    def __init__(self, llm, storage, force: bool = False, limit: int | None = None):
        super().__init__(llm, storage)
        self._limit = limit

    def run(self) -> None:
        st, cfg = self._storage, self._llm._config
        if not st.qwen_prompts_exists():
            print("Error: Missing output/7_qwen-prompts.json. "
                  "Run stage 'qwen-prompts' first.")
            return
        upstream = cfg.motion_path if st.motion_exists() else cfg.scenes_keyframes_path
        if st.is_newer(upstream, cfg.qwen_prompts_path):
            print(f"Error: {upstream.name} is newer than 7_qwen-prompts.json. "
                  f"Run stage 'qwen-prompts' first.")
            return

        data = st.read_qwen_prompts()
        style_bible = (data.get("project") or {}).get("style_bible") or {}
        style = style_bible.get("visual_style", "")
        negatives = style_bible.get("video_negative_terms") or NEGATIVES_VIDEO

        rendered = failed = 0
        t0 = time.perf_counter()
        for _, clip in iter_clips(data):
            video = clip.get("video")
            if not video:
                continue
            if self._limit is not None and rendered >= self._limit:
                break
            try:
                shot = clip.get("shot") or {}
                motion = video.get("motion") or {}
                ctx = {
                    "shot_type":          shot.get("shot_type", "").replace("_", " "),
                    "camera_movement":    shot.get("camera_movement", "static").replace("_", " "),
                    "action_sentence":    motion.get("subject_motion", ""),
                    "secondary_sentence": motion.get("secondary_motion", ""),
                    "ambient_sentence":   motion.get("ambient_motion", ""),
                    "style_text":         style,
                }
                video["rendered_prompt"] = render_sentences(
                    PROMPT_TEMPLATES["ltx_motion"], ctx)
                video["negative_prompt"] = ", ".join(
                    list(negatives) + list(video.get("negative_prompt_extra") or []))
                rendered += 1
            except Exception as exc:
                failed += 1
                print(f"  error on {clip.get('clip_id')}: {exc}")

        st.write_ltx_prompts(data)
        print("\n" + "=" * 60)
        print(f"Stage 8 done in {time.perf_counter() - t0:.2f}s. "
              f"Rendered: {rendered}  Failed: {failed}")
        print(f"File: {cfg.ltx_prompts_path}")