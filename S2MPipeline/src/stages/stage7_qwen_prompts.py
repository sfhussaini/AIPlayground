"""Stage 7 - render image_gen.rendered_prompt for every generated keyframe.

Pure code, no LLM. The ONLY place Qwen prompts are built. Always re-renders
everything, so it can never hold stale text. Every prompt is complete on its
own: environment, identity, outfit, pose, position, light, style.

Lookups (all local to 6_scenes-motion.json, which is a full tree):
  - character          -> data["characters"][0]
  - environment        -> scene["environment"]  (inline per scene)
  - wardrobe state text-> data["wardrobe_states"] by state["wardrobe_state_id"]
  - prop text          -> data["props"] by state["prop_id"]
  - pose text          -> reference.POSES by state["pose_id"]

Sentinel IDs ('default', 'none') ALWAYS render as empty text. Stage 3b
already ensures this by deduping with the sentinel, but we reassert here so
that a hand-edited 3_scenes.json cannot accidentally inject text into a
'default' state and make Qwen paint the character in a non-default outfit.

Delta suppression (framing_text only):
  - Qwen Image Edit uses the previous keyframe as its source image.
  - Repeating "Framing: ..." against an already-reframed reference pushes
    the model to re-frame further, drifting at each step. Suppressing
    framing_text when unchanged stops this.
  - light_text is NO LONGER suppressed. Always re-assert lighting so Qwen
    gets an explicit cue to preserve brightness.
"""
import time
from typing import Optional

from reference import POSES
from templates import (
    DEPTH_LABELS, FACING_LABELS, HORIZONTAL_LABELS, NEGATIVES_STILL,
    PROMPT_TEMPLATES,
)
from .base import PipelineStage, iter_clips, render_sentences


_DELTA_FIELDS = ("framing_text",)

# Reserved sentinel IDs. Their text is force-blanked in _build_ctx regardless
# of what the library says. This is belt-and-braces: Stage 3b's _merge_library
# already dedupes with empty-text sentinels winning. But a hand-edited
# 3_scenes.json could re-introduce non-empty text under these IDs, and we
# don't want Qwen to paint a default wardrobe as "wet clothes" etc.
_SENTINEL_WARDROBE_IDS = frozenset({"default"})
_SENTINEL_PROP_IDS = frozenset({"none"})


class Stage7QwenPrompts(PipelineStage):
    def __init__(self, llm, storage, force: bool = False, limit: int | None = None):
        super().__init__(llm, storage)
        self._limit = limit

    def run(self) -> None:
        st, cfg = self._storage, self._llm._config

        if st.motion_exists() and not st.is_newer(cfg.scenes_keyframes_path,
                                                   cfg.motion_path):
            data = st.read_motion()
            print(f"Loaded {cfg.motion_path}")
        elif st.scenes_keyframes_exists():
            data = st.read_scenes_keyframes()
            print(f"Loaded {cfg.scenes_keyframes_path} "
                  f"(motion not run yet or out of date)")
        else:
            print("Error: Missing output/5_scenes-keyframes.json. "
                  "Run stage 'keyframes' first.")
            return

        for key in ("project", "characters", "wardrobe_states", "props", "scenes"):
            if key not in data:
                print(f"Error: input tree missing '{key}'. "
                      f"Re-run 'keyframes'.")
                return

        style_bible = (data.get("project") or {}).get("style_bible") or {}
        negative = ", ".join(style_bible.get("global_negative_terms")
                              or NEGATIVES_STILL)

        wardrobe_text_by_id = {s["state_id"]: s["text"]
                                for s in data["wardrobe_states"]}
        prop_text_by_id = {p["prop_id"]: p["text"] for p in data["props"]}
        pose_text_by_id = {p["pose_id"]: p["text"] for p in POSES}
        pose_text_by_id["pose_none"] = "no person in the frame"

        # Belt-and-braces: sentinel text is always empty, whatever the library
        # (or a hand edit) says.
        for sid in _SENTINEL_WARDROBE_IDS:
            wardrobe_text_by_id[sid] = ""
        for pid in _SENTINEL_PROP_IDS:
            prop_text_by_id[pid] = ""

        rendered = failed = suppressed = 0
        last_ctx: Optional[dict] = None
        t0 = time.perf_counter()

        for scene, clip in iter_clips(data):
            for role in ("first", "middle", "last"):
                kf = (clip.get("keyframes") or {}).get(role)
                if not kf:
                    continue
                if kf.get("reuse_from"):
                    continue
                ig = kf.get("image_gen")
                if not ig:
                    continue
                if self._limit is not None and rendered >= self._limit:
                    ig["rendered_prompt"] = None
                    continue
                try:
                    full_ctx = self._build_ctx(
                        kf, clip, scene, data, style_bible,
                        wardrobe_text_by_id, prop_text_by_id,
                        pose_text_by_id,
                    )
                    prompt_ctx = dict(full_ctx)

                    is_chain_start = (role == "first")
                    if not is_chain_start and last_ctx is not None:
                        for key in _DELTA_FIELDS:
                            if prompt_ctx.get(key) == last_ctx.get(key):
                                prompt_ctx[key] = ""
                                suppressed += 1

                    ig["rendered_prompt"] = render_sentences(
                        PROMPT_TEMPLATES[ig["template_id"]], prompt_ctx)
                    ig["negative_prompt"] = negative
                    last_ctx = full_ctx
                    rendered += 1
                except Exception as exc:
                    failed += 1
                    print(f"  error on {kf.get('keyframe_id')}: {exc}")

        st.write_qwen_prompts(data)
        print("\n" + "=" * 60)
        print(f"Stage 7 done in {time.perf_counter() - t0:.2f}s. "
              f"Rendered: {rendered}  Failed: {failed}  "
              f"Suppressed fields: {suppressed}")
        print(f"File: {cfg.qwen_prompts_path}")

    # ------------------------------------------------------------------
    @staticmethod
    def _build_ctx(kf, clip, scene, data,
                   style_bible,
                   wardrobe_text_by_id: dict,
                   prop_text_by_id: dict,
                   pose_text_by_id: dict) -> dict:
        state = kf.get("state") or {}

        character = next(
            (c for c in data.get("characters", [])
             if c["character_id"] == state.get("character_id")),
            None,
        )
        wardrobe = next(
            (w for w in (character or {}).get("wardrobe", [])
             if w["wardrobe_id"] == clip.get("wardrobe_id")),
            None,
        )
        env = scene.get("environment") or {}

        pose_text = pose_text_by_id.get(state.get("pose_id", ""), "")
        prop_text = prop_text_by_id.get(state.get("prop_id", ""), "")
        wardrobe_state_text = wardrobe_text_by_id.get(
            state.get("wardrobe_state_id", ""), "")

        shot = clip.get("shot") or {}
        light = scene.get("light") or {}

        h = HORIZONTAL_LABELS.get(state.get("screen_horizontal", ""), "")
        position = ", ".join(x for x in (
            f"{h} of the frame" if h else "",
            DEPTH_LABELS.get(state.get("screen_depth", ""), ""),
            FACING_LABELS.get(state.get("facing", ""), ""),
        ) if x)

        framing = ""
        if shot.get("shot_type"):
            framing = (f"{shot['shot_type'].replace('_', ' ')} shot, "
                       f"{shot.get('camera_angle', '').replace('_', ' ')}, "
                       f"{shot.get('lens_mm', '')}mm lens")

        style = style_bible.get("visual_style", "")
        grade = style_bible.get("color_grade_default", "")
        if style and grade:
            style = f"{style}, {grade} color grade"

        return {
            "subject":                 (character or {}).get("subject", ""),
            "environment_description": env.get("description", ""),
            "identity_lock":           (character or {}).get("identity_lock", ""),
            "wardrobe_text":           (wardrobe or {}).get("text", ""),
            "wardrobe_state_text":     wardrobe_state_text,
            "framing_text":            framing,
            "pose_text":               pose_text,
            "position_text":           position,
            "expression_text":         state.get("expression", ""),
            "prop_text":               prop_text,
            "light_text":              ", ".join(
                x for x in (light.get(k) for k in
                             ("source", "direction", "quality", "color_temp"))
                if x),
            "style_text":              style,
        }