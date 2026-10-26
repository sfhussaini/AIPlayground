"""StoryPipeline - the orchestrator.

Fragments-first flow:
  1.5  characters   (Vision, per-PNG)                  [human review]
  3a   breakdown    (LLM, whole story)                 [human review gate]
  1    project      (LLM, first N words)
  3b   scenes       (code, fragments+sheet+project)    denormalizes libraries
  4    shots        (LLM, per clip)
  5    keyframes    (LLM, per clip)                    +pose escape hatch review
  6    motion       (LLM, per clip)
  7    qwen-prompts (code)
  8    ltx-prompts  (code)
  9    qwen-export  (code)

No 2_assets.json. Per-story libraries (environments, wardrobe_states,
props) come from the fragmenter and are denormalized into 3_scenes.json.
Character data comes from input/char_XX_sheet.json. Pose library stays
in reference/poses.json.
"""
from config import PipelineConfig
from llm_client import LLMClient
from storage import StoryStorage
from validate import ALL_STAGES, check_stage, report
from stages.stage1_5_characters import Stage1_5Characters
from stages.stage1_project      import Stage1Project
from stages.stage3_breakdown    import Stage3Breakdown
from stages.stage3b_build_scenes import Stage3bBuildScenes
from stages.stage4_shots        import Stage4Shots
from stages.stage5_keyframes    import Stage5Keyframes
from stages.stage6_motion       import Stage6Motion
from stages.stage7_qwen_prompts import Stage7QwenPrompts
from stages.stage8_ltx_prompts  import Stage8LtxPrompts


class StoryPipeline:
    def __init__(self, config: PipelineConfig | None = None):
        self.config  = config or PipelineConfig()
        self.llm     = LLMClient(self.config)
        self.storage = StoryStorage(self.config)

    # --- character extraction (Vision) ---------------------------------
    def extract_characters(self, force: bool = False) -> None:
        Stage1_5Characters(self.llm, self.storage, force=force).run()

    # --- Stage 3a: fragmentation (whole-story LLM + human review) ------
    def breakdown_story(self, re_fragment: bool = False,
                        refine_id: str | None = None) -> None:
        Stage3Breakdown(
            self.llm, self.storage,
            re_fragment=re_fragment, refine_id=refine_id,
        ).run()

    # --- structural stages ---------------------------------------------
    def generate_project(self, force: bool = False) -> None:
        Stage1Project(self.llm, self.storage, force=force).run()

    def build_scenes(self, force: bool = False) -> None:
        Stage3bBuildScenes(self.llm, self.storage, force=force).run()

    # --- filling stages (4-6) ------------------------------------------
    def fill_shots(self, limit: int | None = None) -> None:
        Stage4Shots(self.llm, self.storage, limit=limit).run()

    def fill_keyframes(self, limit: int | None = None) -> None:
        Stage5Keyframes(self.llm, self.storage, limit=limit).run()

    def fill_motion(self, limit: int | None = None) -> None:
        Stage6Motion(self.llm, self.storage, limit=limit).run()

    # --- rendering stages (7-8) + export -------------------------------
    def fill_qwen_prompts(self, force: bool = False,
                          limit: int | None = None) -> None:
        Stage7QwenPrompts(self.llm, self.storage,
                          force=force, limit=limit).run()

    def fill_ltx_prompts(self, force: bool = False,
                         limit: int | None = None) -> None:
        Stage8LtxPrompts(self.llm, self.storage,
                         force=force, limit=limit).run()

    def export_qwen_images(self) -> int:
        from util.qwen import export
        return export(self.config)

    # --- validation ----------------------------------------------------
    def validate(self, stages: list[str] | None = None) -> int:
        errors = 0
        for key in (stages or ALL_STAGES):
            errors += report(check_stage(self.storage, key), key)
        return errors

    def _gate(self, key: str, abort: bool = True) -> bool:
        errors = report(check_stage(self.storage, key), key)
        return not (abort and errors)

    def _characters_ready(self) -> bool:
        """True if at least one character sheet JSON or legacy character.json
        exists. Only checks presence; the review gate is enforced later."""
        for _, raw in self.storage.iter_character_sheet_jsons():
            if raw is not None:
                return True
        return self.storage.character_exists()

    # --- end-to-end ----------------------------------------------------
    def run_all(self, force: bool = False) -> None:
        # 1.5 Characters
        self.extract_characters(force=force)
        if not self._characters_ready():
            print("Aborted: no character sheet JSON was produced.")
            print("  Place input/char_XX_sheet.png, then run "
                  "'python main.py characters'.")
            return

        # 3a Fragmentation - the ONLY whole-story call.
        # It authors environments, wardrobe_states, props alongside the
        # clip hierarchy.
        self.breakdown_story()
        if not self.storage.fragmented_exists():
            print("Aborted: fragmentation did not produce output.")
            return

        fragments = self.storage.read_fragmented()
        reviewed = bool((fragments.get("review") or {}).get("reviewed"))
        if not reviewed and self.config.require_fragmentation_review:
            print("\nAborted at the fragmentation review gate.")
            print(f"  Inspect {self.config.fragmented_path},")
            print(f"  set review.reviewed to true,")
            print(f"  then re-run: python main.py all")
            return
        if not reviewed:
            print(f"Warning: proceeding with unreviewed fragments "
                  f"(require_fragmentation_review=false).")

        # 1 Project
        self.generate_project(force=force)
        if not self.storage.project_exists():
            print("Aborted: project stage did not produce output.")
            return

        # 3b Build scenes (pure code - denormalizes libraries into scenes)
        self.build_scenes(force=force)
        if not self.storage.scenes_exists():
            print("Aborted: scenes build did not produce output.")
            return
        if not self._gate("breakdown"):
            print("Aborted: scenes produced invalid output.")
            return

        # 4-6 Filling stages (per clip)
        self.fill_shots()
        self.fill_keyframes()
        self._gate("keyframes", abort=False)
        self.fill_motion()

        # 7-8 Rendering stages (pure code)
        self.fill_qwen_prompts()
        self._gate("qwen-prompts", abort=False)
        self.fill_ltx_prompts()

        # 9 Export
        self.export_qwen_images()