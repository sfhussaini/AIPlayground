"""Centralized, injectable pipeline configuration.

New flow (fragments-first):
  - Stage 3a (LLM) reads the WHOLE story once → input/story_fragmented.json.
  - Stage 1 (project) reads the first `intro_words` of story.txt.
  - Stage 2 (assets) reads fragments only: envs consolidated by slug,
    props per scene, poses from deduped action labels, wardrobe states
    proposed per wardrobe from clip actions.
  - Stage 3b (code) builds output/3_scenes.json from fragments + assets.
  - Stages 4-8 are per-clip / per-scene.

`reference/` is story-independent: vocabularies, presets, labels, templates,
and poses.json (a growing universal library). Environments, props, and
wardrobe states are per-story and live ONLY in output/2_assets.json.

Pose creation uses its own model/temperature knob (see pose_model_name).
Wardrobe state proposals use their own temperature (see state_temperature).
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PipelineConfig:
    # --- LLM connection (text) ---
    lm_studio_url: str = "http://localhost:1234/v1"
    model_name:    str = "qwen3.5-9b-aggressive"
    api_key:       str = "1234"
    reasoning_effort: str | None = None

    # --- LLM connection (vision) ---
    vision_model_name:  str   = model_name
    vision_temperature: float = 0.2
    vision_max_image_mb: int  = 4

    # --- LLM connection (pose creation: creative, story-independent) ---
    pose_model_name:  str   = model_name
    pose_temperature: float = 0.7

    # --- Character extraction (Stage 1.5) ---
    resolve_uncertain_fields:    bool = True
    character_field_retries:     int  = 2
    interactive_character_review: bool = True

    # --- Stage 2 pose review ---
    interactive_pose_review: bool = True
    pose_match_threshold:    float = 0.75
    persist_new_poses:       bool = True

    # --- Stage 2 wardrobe-state review ---
    interactive_state_review: bool  = True
    state_temperature:        float = 0.4

    # --- Stage 3 lighting controls ---
    force_time_of_day:        str | None = None
    dark_time_warn_threshold: float      = 0.5
    clamp_dark_lighting:      bool       = False

    # --- Stage 3a fragmentation (LLM, human-reviewed) ---
    require_fragmentation_review: bool  = True
    fragmentation_temperature:    float = 0.3

    max_concurrency: int = 1

    # --- Filesystem layout ---
    input_dir:  str = "input"
    output_dir: str = "output"

    story_filename:      str = "story.txt"
    fragmented_filename: str = "story_fragmented.json"
    character_filename:  str = "character.json"

    project_filename:          str = "1_project.json"
    assets_filename:           str = "2_assets.json"
    scenes_filename:           str = "3_scenes.json"
    scenes_shots_filename:     str = "4_scenes-shots.json"
    scenes_keyframes_filename: str = "5_scenes-keyframes.json"
    motion_filename:           str = "6_scenes-motion.json"
    qwen_prompts_filename:     str = "7_qwen-prompts.json"
    ltx_prompts_filename:      str = "8_ltx-prompts.json"
    qwen_export_filename:      str = "9_qwen-images.json"

    # --- Temperatures ---
    project_temperature:   float = 0.1
    assets_temperature:    float = 0.2
    shots_temperature:     float = 0.4
    keyframes_temperature: float = 0.4
    motion_temperature:    float = 0.6

    # --- Small-model limits ---
    max_context_words:   int = 500
    intro_words:         int = 250
    max_clips:           int = 60

    require_character_review: bool = False

    interactive_clip_selection: bool = True
    default_timeout:            int  = 300
    llm_max_retries:            int  = 3

    # --- Derived paths ---
    @property
    def input_path(self) -> Path:            return Path(self.input_dir)
    @property
    def story_path(self) -> Path:            return self.input_path / self.story_filename
    @property
    def fragmented_path(self) -> Path:       return self.input_path / self.fragmented_filename
    @property
    def character_path(self) -> Path:        return self.input_path / self.character_filename
    @property
    def project_path(self) -> Path:          return Path(self.output_dir) / self.project_filename
    @property
    def assets_path(self) -> Path:           return Path(self.output_dir) / self.assets_filename
    @property
    def scenes_path(self) -> Path:           return Path(self.output_dir) / self.scenes_filename
    @property
    def scenes_shots_path(self) -> Path:     return Path(self.output_dir) / self.scenes_shots_filename
    @property
    def scenes_keyframes_path(self) -> Path: return Path(self.output_dir) / self.scenes_keyframes_filename
    @property
    def motion_path(self) -> Path:           return Path(self.output_dir) / self.motion_filename
    @property
    def qwen_prompts_path(self) -> Path:     return Path(self.output_dir) / self.qwen_prompts_filename
    @property
    def ltx_prompts_path(self) -> Path:      return Path(self.output_dir) / self.ltx_prompts_filename
    @property
    def qwen_export_path(self) -> Path:      return Path(self.output_dir) / self.qwen_export_filename