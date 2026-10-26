"""Stage 1 - project rules & style bible. The LLM only PICKS from lists.

Runs AFTER fragments in the new flow (fragments-first), but is independent
of them: still reads the first `intro_words` of story.txt directly.
Reference data (STYLE_PRESETS, COLOR_GRADES, NEGATIVES_STILL, NEGATIVES_VIDEO)
lives in <project_root>/reference/*.json, loaded by reference.py. RISCOR
prompts remain in templates.py - they are logic, not data.

Output (output/1_project.json) shape is unchanged: title, style_preset,
color_grade, and a fully expanded style_bible that downstream stages
(7_qwen-prompts, 8_ltx-prompts) consume via the assembled tree.

intro_words is a cap, not a fixed slice: stories at or below the cap are used
whole ("Using the whole story."); longer stories are truncated to the first
intro_words for the style pick.
"""
import re
import time

from langchain_core.prompts import ChatPromptTemplate

from models import ProjectLLM
from reference import (
    COLOR_GRADES, NEGATIVES_STILL, NEGATIVES_VIDEO, STYLE_PRESETS,
)
from templates import PROJECT_RULES_SYSTEM, PROJECT_RULES_USER
from .base import PipelineStage, window_at


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "movie"


class Stage1Project(PipelineStage):
    def __init__(self, llm, storage, force: bool = False):
        super().__init__(llm, storage)
        self._force = force

    def run(self) -> None:
        if self._force:
            self._storage.delete_project()
        if self._storage.project_exists():
            print("Stage 1 already complete (output/1_project.json). "
                  "Use --force to regenerate.")
            return
        if not self._check_inputs(need_story=True):
            return

        cfg = self._llm._config
        story = self._storage.read_story()
        total_words = len(story.split())

        if total_words <= cfg.intro_words:
            intro = story
            print(f"Story loaded ({total_words} words). Using the whole story.")
        else:
            intro, _ = window_at(story, 0, cfg.intro_words)
            print(f"Story loaded ({total_words} words). Using the first "
                  f"{len(intro.split())} words.")

        llm = self._llm.structured(ProjectLLM, temperature=cfg.project_temperature)
        prompt = ChatPromptTemplate.from_messages([
            ("system", PROJECT_RULES_SYSTEM),
            ("human",  PROJECT_RULES_USER),
        ])
        chain = prompt | llm

        print("Calling LLM for project rules...")
        t0 = time.perf_counter()
        try:
            result: ProjectLLM = self._invoke_with_retry(chain, {
                "story":         intro,
                "style_presets": ", ".join(STYLE_PRESETS),
                "color_grades":  ", ".join(COLOR_GRADES),
            }, prompts=(PROJECT_RULES_SYSTEM, PROJECT_RULES_USER))
        except Exception as exc:
            print(f"Error: {type(exc).__name__}: {exc}")
            return
        print(f"LLM project rules took {time.perf_counter() - t0:.2f}s")

        preset = STYLE_PRESETS[result.style_preset]
        title = result.title.strip()
        data = {
            "schema_version": "2.0",
            "project": {
                "movie_id":          _slug(title),
                "title":             title,
                "clip_duration_sec": 5,
                "style_bible": {
                    "style_preset":          result.style_preset,
                    "visual_style":          preset["art_medium_style"],
                    "look_keywords":         [k.strip() for k in preset["quality_tags"].split(",")],
                    "color_grade_default":   result.color_grade,
                    "global_negative_terms": list(NEGATIVES_STILL),
                    "video_negative_terms":  list(NEGATIVES_VIDEO),
                },
                "active_profile": "draft_12gb",
            },
        }
        self._storage.write_project(data)
        print(f"Wrote {self._storage._config.project_path}")