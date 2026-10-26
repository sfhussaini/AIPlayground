# Story → Video Data Flow Pipeline — Final Design Document

**Version:** 1.0
**Status:** Approved — ready for implementation
**Last revised:** 2026-10-22

---

## 1. Purpose

A six-stage pipeline that converts a plain-text story into a fully-structured JSON plan (`6_final.json`) for downstream Qwen Image Edit (keyframe generation) and LTX I2V (video generation). The pipeline is designed around a **2B-parameter LLM** as the prompt author, a **single character sheet** as the only pre-existing image asset, and **12 GB VRAM** downstream.

The LLM fills slots. Code renders prompts, assigns IDs, seeds, and job objects. All decisions are made to keep the small model inside a narrow, deterministic envelope.

---

## 2. Architecture

Same layered pattern as the existing project:

```
main.py  →  pipeline.py  →  stages/stageN.py  →  llm_client.py / storage.py / models.py
                                    ↓
                              templates.py (RISCOR prompts + constants)
                                    ↓
                              config.py (PipelineConfig)
```

**Layers:**

| Layer | Responsibility | Never does |
|---|---|---|
| `main.py` | CLI argument parsing | Business logic |
| `pipeline.py` | Stage orchestration, prerequisites | Direct I/O |
| `stages/*` | One stage each; LLM call + merge + save | Cross-stage wiring |
| `llm_client.py` | LangChain model construction | Business logic |
| `storage.py` | Filesystem I/O, atomic writes, traversal | LLM calls |
| `models.py` | Pydantic schemas for LLM structured output | I/O |
| `templates.py` | RISCOR prompts + closed vocabularies + style presets | Anything runtime |
| `config.py` | Injectable configuration | I/O |

**Core principle:** the 2B model fills slots only. Numeric IDs, timestamps, seeds, `image_gen` and `video` job objects, `reuse_from`, and prompt rendering are all code-owned.

---

## 3. Data Flow

```
input/story.txt              ← user-provided (never written by pipeline)
input/char_01_sheet.png      ← user-provided (one per character)
        │
        ▼
[Stage 1: Project Rules]   read input/story.txt
        │                  write output/1_project.json
        ▼
[Stage 2: Assets]          read output/1 + input/story.txt
        │                        + input/char_*_sheet.png (path discovery only)
        │                  write output/2_assets.json
        ▼
[Stage 3: Breakdown]       read output/1 + output/2 + input/story.txt
        │                  write output/3_scenes.json
        ▼
[Stage 4: Shots]           read output/3 + output/2
        │                        + [CLIP SELECTION]
        │                  write output/4_scenes-shots.json
        ▼
[Stage 5: Keyframes]       read output/4 + output/2
        │                        + [CLIP SELECTION]
        │                  write output/5_scenes-keyframes.json
        ▼
[Stage 6: Motion]          read output/5 + output/1 + output/2 + constants
        │                        + [CLIP SELECTION]
        │                  write output/6_final.json
```

Each stage fails fast if its prerequisites are missing.

---

## 4. Folder Structure

```
project_root/
├── __init__.py                  Exports PipelineConfig, StoryPipeline
├── main.py                      CLI
├── config.py                    PipelineConfig dataclass
├── llm_client.py                LangChain wrapper
├── models.py                    Pydantic schemas
├── storage.py                   Filesystem I/O + traversal
├── pipeline.py                  Stage orchestration facade
├── templates.py                 RISCOR prompts + VOCABULARIES + STYLE_PRESETS
├── README.md                    User-facing contract
└── stages/
    ├── __init__.py
    ├── base.py                  PipelineStage ABC + clip selection helpers
    ├── stage1_project.py
    ├── stage2_assets.py
    ├── stage3_breakdown.py
    ├── stage4_shots.py
    ├── stage5_keyframes.py
    └── stage6_motion.py

input/                           USER-PROVIDED — pipeline never writes here
├── story.txt                    Required
├── char_01_sheet.png            Required (one per character)
└── char_02_sheet.png            Optional

output/                          PIPELINE-GENERATED
├── tmp/                         Atomic-write staging (auto-managed)
├── 1_project.json
├── 2_assets.json
├── 3_scenes.json
├── 4_scenes-shots.json
├── 5_scenes-keyframes.json
└── 6_final.json
```

**`input/` vs `output/` contract:**

| Folder | Written by | Purpose |
|---|---|---|
| `input/` | User only | Story text + character sheets |
| `output/` | Pipeline only | Stage artifacts, JSON |

`output/tmp/` is transient — used only during atomic writes.

---

## 5. Stage Specifications

### Stage 1 — Project Rules

| Field | Value |
|---|---|
| **Reads** | `input/story.txt` |
| **Writes** | `output/1_project.json` |
| **Type** | Structural (one-shot) |
| **LLM** | 1 structured call → `ProjectRules` |
| **Temperature** | 0.4 |
| **Resume** | File exists → skip; `--force` regenerates |

**Output shape:**
```json
{
  "schema_version": "2.0",
  "project": {
    "movie_id": "…",
    "title": "…",
    "target_runtime_min": 2.0,
    "clip_duration_sec": 5,
    "style_bible": {
      "visual_style": "…",
      "look_keywords": ["…"],
      "color_grade_default": "…",
      "global_negative_terms": ["jump cut", "teleport", "…"]
    },
    "active_profile": "draft_12gb"
  }
}
```

**Fail conditions:** story missing or empty.

---

### Stage 2 — Assets

| Field | Value |
|---|---|
| **Reads** | `output/1_project.json`, `input/story.txt`, sheet discovery |
| **Writes** | `output/2_assets.json` |
| **Type** | Structural (one-shot) |
| **LLM** | 1 structured call → `AssetsBundle` |
| **Temperature** | 0.7 |
| **Resume** | File exists → skip; `--force` regenerates |

**Output shape:** `{ "schema_version": "2.0", "assets": { "characters": [...], "environments": [...], "props": [...], "pose_library": [...] } }`

**Post-processing (code, not LLM):**

1. Discover sheets with `storage.discover_character_sheets()` → `{char_01: Path("input/char_01_sheet.png")}`
2. **Hard fail** if `assets.characters` is non-empty AND no sheets were found. Print:
   ```
   Error: Story has characters but no character sheets found in input/.
   Expected format: input/char_01_sheet.png (one per character).
   ```
3. **Warn** if multiple characters — only `characters[0]` will be used in stages 4–6.
4. Attach `sheet.path` + `sheet.status` to each character.
5. Attach default `plates[]` to each environment (one plate per env, `noon_medium_wide`).

**Fail conditions:** story missing, project missing, characters present but no sheets.

---

### Stage 3 — Breakdown

| Field | Value |
|---|---|
| **Reads** | `output/1` + `output/2` + `input/story.txt` |
| **Writes** | `output/3_scenes.json` |
| **Type** | Structural (one-shot) |
| **LLM** | 1 structured call → `BreakdownBundle` |
| **Temperature** | 0.5 |
| **Resume** | File exists → skip; `--force` regenerates |

**Clip budget** passed to LLM: `target_runtime_min × 60 ÷ 5`. Soft target — accepted with ±50% tolerance; aborts only if breakdown is wildly off (e.g. 10× budget).

**Output shape:**
```json
{
  "schema_version": "2.0",
  "scenes": [
    {
      "scene_id": "s01",
      "scene_number": 1,
      "name": "…",
      "summary": "…",
      "environment_ref": { "environment_id": "env_…", "plate_id": "env_….noon_medium_wide" },
      "time_of_day": "…",
      "weather": "…",
      "light": { "source": "…", "direction": "…", "quality": "…", "color_temp": "…" },
      "characters_present": ["char_01"],
      "ambience": "…",
      "segments": [
        {
          "segment_id": "s01.sg01",
          "segment_number": 1,
          "name": "…",
          "beat_summary": "…",
          "emotional_arc": "…",
          "clips": [
            {
              "clip_id": "s01.sg01.c001",
              "clip_number": 1,
              "name": "…",
              "story_action": "…",
              "duration_sec": 5,
              "continuity": { "transition_in": "hard_cut", "framing_locked": true },
              "wardrobe_id": "wd_01"
            }
          ]
        }
      ]
    }
  ]
}
```

**Fail conditions:** story missing, project missing, assets missing, 0 scenes produced.

---

### Stage 4 — Shots (Filling)

| Field | Value |
|---|---|
| **Reads** | `output/3_scenes.json` + `output/2_assets.json` |
| **Writes** | `output/4_scenes-shots.json` |
| **Type** | Filling (per clip) |
| **LLM** | N structured calls → `Shot`, one per selected clip |
| **Temperature** | 0.4 |
| **Concurrency** | `ThreadPoolExecutor(max_workers=config.max_concurrency)` |
| **Resume** | File exists → load; per-clip done marker = `clip.shot.shot_type` non-empty |

**Per-clip LLM context passed:** clip name/action/wardrobe, scene name/time/weather/light summary, segment name/beat/arc, characters present, and all four vocabularies (`shot_type`, `camera_angle`, `lens_mm`, `camera_movement`).

**After each LLM return:** `clip["shot"] = shot.model_dump()`, then `storage.write_scenes_shots(clipped)`. Save-per-clip; no batch write.

---

### Stage 5 — Keyframes (Filling)

| Field | Value |
|---|---|
| **Reads** | `output/4_scenes-shots.json` + `output/2_assets.json` |
| **Writes** | `output/5_scenes-keyframes.json` |
| **Type** | Filling (per clip) |
| **LLM** | N structured calls → `KeyframesLLM`, one per selected clip |
| **Temperature** | 0.5 |
| **Resume** | File exists → load; per-clip done marker = `clip.keyframes.last.state.pose_id` non-empty |

**Per-clip LLM context passed:** clip name/action/wardrobe/shot summary, characters present, character_id, available poses, available wardrobe states for the clip's wardrobe_id, available props, and the closed lists for `screen_horizontal`, `screen_depth`, `facing`, `expression`, `wetness`, `mud_level`, `hair_state`.

**Post-processing (code, not LLM) — builds full `clip.keyframes` dict:**

For each role in `first / middle / last`:

| Field | Source |
|---|---|
| `keyframe_id` | `f"{clip_id}.{role}"` |
| `timestamp_sec` | `0.0 / 2.5 / 5.0` |
| `reuse_from` | `previous_clip.last.keyframe_id` if `transition_in=="continuous_from_previous"` **and** previous clip is in same scene; else `None` |
| `delta_from_previous` | LLM output (`None` for first) |
| `must_preserve_extra` | LLM output |
| `state` | LLM output (omitted if reused) |
| `image_gen` | Code-built (omitted if reused) |

**`image_gen` construction:**

| Role | `template_id` | `inputs[0]` (canvas) | `inputs[1]` (identity_ref) |
|---|---|---|---|
| first (new) | `qwen_first_in_scene` | `plate:<env plate_id>` | `sheet:char_XX` |
| first (reused) | — | (no `image_gen`) | — |
| middle | `qwen_chain` | `keyframe:<clip_id>.first` | `sheet:char_XX` |
| last | `qwen_chain` | `keyframe:<clip_id>.middle` | `sheet:char_XX` |

**Seed:** `int.from_bytes(sha256(f"{clip_id}.{role}.qwen").digest()[:4])` — deterministic.

**Multi-character handling (C1):** only `scene.characters_present[0]` is processed. If >1 character present, prints a warning and proceeds with the first.

**Zero-character clips (C2):** `pose_id="pose_none"`, `character_id=""`, only screen position + condition filled.

---

### Stage 6 — Motion (Filling)

| Field | Value |
|---|---|
| **Reads** | `output/5` + `output/1` + `output/2` + `templates.py` constants |
| **Writes** | `output/6_final.json` |
| **Type** | Filling (per clip) |
| **LLM** | N structured calls → `VideoLLM`, one per selected clip |
| **Temperature** | 0.6 |
| **Resume** | File exists → load; per-clip done marker = `clip.video.motion.subject_motion` non-empty |

**Per-clip LLM context passed:** clip name/action/shot summary, three keyframe summaries (pose + position + expression), scene ambience.

**Post-processing — builds full `clip.video` dict:**

```json
{
  "task": "ltx_fml",
  "template_id": "ltx_motion",
  "inputs": {
    "first":  "keyframe:<clip_id>.first",
    "middle": "keyframe:<clip_id>.middle",
    "last":   "keyframe:<clip_id>.last"
  },
  "motion": { "subject_motion": "…", "secondary_motion": "…", "ambient_motion": "…" },
  "audio":  { "ambience": "…", "sfx": "…", "dialogue": null },
  "negative_prompt_extra": [],
  "seed": <sha256(f"{clip_id}.ltx")[:4]>,
  "rendered_prompt": null,
  "status": "pending",
  "output_path": null
}
```

**`6_final.json` — merged final artifact:**

```json
{
  "schema_version": "2.0",
  "project":           <from 1_project.json>,
  "vocabularies":      <from templates.py VOCABULARIES>,
  "prompt_templates":  <from templates.py PROMPT_TEMPLATES>,
  "style_presets":     <from templates.py STYLE_PRESETS>,
  "assets":            <from 2_assets.json>,
  "scenes":            <from 5_scenes-keyframes.json with video added>
}
```

---

## 6. RISCOR Prompts — Summary

Each stage's LLM call uses a **RISCOR** system prompt (Role / Instruction / Steps / Context / Output / Rules) written in **child-like language** so the 2B model follows literally. Full prompts live in `templates.py`.

| Stage | System prompt | Focus |
|---|---|---|
| 1 | `PROJECT_RULES_SYSTEM` | Big rules only — style, palette, negatives. No characters, no story. |
| 2 | `ASSETS_SYSTEM` | Character identity, environments, props, poses. `identity_lock` sentence is mandatory. |
| 3 | `BREAKDOWN_SYSTEM` | Scenes → segments → clips. Each clip = exactly one 5-second action. |
| 4 | `SHOTS_SYSTEM` | One clip's camera: shot_type, angle, lens, movement, focus. Lens picks coupled to shot_type. |
| 5 | `KEYFRAMES_SYSTEM` | Three pictures per clip, **one big change per picture**. First has `delta=null`. |
| 6 | `MOTION_SYSTEM` | Full sentences in chronological order. Present tense. No camera directions. |

**Universal rules** applied in every system prompt:
- Only use values from my list. Do not invent.
- Do not add fields I did not ask for.
- Use simple, concrete words. "Oval face" good, "chiseled" bad.

---

## 7. Resume Logic

### Stage Level

| Stage | Check | Behavior |
|---|---|---|
| 1 | `output/1_project.json` exists | Skip; `--force` → delete + regenerate |
| 2 | `output/2_assets.json` exists | Skip; `--force` → delete + regenerate |
| 3 | `output/3_scenes.json` exists | Skip; `--force` → delete + regenerate |
| 4 | `output/4_scenes-shots.json` exists | Load & continue (per-clip resume handles the rest) |
| 5 | `output/5_scenes-keyframes.json` exists | Load & continue |
| 6 | `output/6_final.json` exists | Load, add pending clips, rewrite |

`--force` is accepted only on stages 1, 2, 3. Filling stages use clip selection to reprocess `[DONE]` clips.

### Within-Stage

| Stage | Done marker per clip |
|---|---|
| 4 | `clip["shot"]["shot_type"]` is a non-empty string |
| 5 | `clip["keyframes"]["last"]["state"]["pose_id"]` is a non-empty string |
| 6 | `clip["video"]["motion"]["subject_motion"]` is a non-empty string |

### Atomic Writes

All writes go through `output/tmp/<name>.<pid>.tmp` → `os.replace()` → final path. No partial-write corruption even on SIGKILL.

---

## 8. Interactive Clip Selection

Filling stages (4, 5, 6) prompt for a CSV of clip numbers before processing.

### Display

```
============================================================
Stage 4 — Clip Selection (Shots)
============================================================

Scene s01 — rice paddy at dawn (3 clips, 2 pending)
  [ 1] [    ] s01.sg01.c001 — walking to shade
  [ 2] [DONE] s01.sg01.c002 — sitting down
  [ 3] [    ] s01.sg01.c003 — opening tiffin

Scene s02 — river bank at midday (2 clips, 2 pending)
  [ 4] [    ] s02.sg01.c001 — washing hands
  [ 5] [    ] s02.sg01.c002 — drinking water

============================================================
Total: 5 clips, 4 pending

Enter CSV of clip numbers ('q' to quit):
  Examples: '1,3'  '1-3'  '1,3-4,5'  'all'  'pending'  'none'
> _
```

### Accepted Inputs

| Input | Meaning |
|---|---|
| *(empty)* or `pending` | All pending clips |
| `all` | All clips, including `[DONE]` (reprocess) |
| `none` | Process nothing; stage exits cleanly |
| `q` | Quit immediately (no summary) |
| `1,3,5` / `1-5` / `1,3-5,7` | Numeric selections |
| Anything else | Error, re-prompt |

### Behavior Table

| Scenario | Behavior |
|---|---|
| Select a `[DONE]` clip | Reprocessed and overwritten |
| Invalid number | Silently dropped; re-prompt if nothing remains |
| Malformed CSV | `ValueError` caught; re-prompt |
| Empty input | All pending; `[DONE]` untouched |
| `q` | No writes for unselected clips; saved progress preserved |
| `--all-clips` flag | Select all pending; skip prompt |
| Non-TTY stdin | Auto-fallback to `--all-clips` behavior |
| Clip fails mid-processing | Error printed; clip stays `[    ]`; next run retries |

### Helper API (`stages/base.py`)

```python
_prompt_clip_selection(clips_with_status, stage_label) -> set[int] | None
_parse_clip_csv(spec: str, total: int) -> set[int]   # raises ValueError
_clips_for_stage(scenes_data, done_fn) -> list[(num, scene, segment, clip, is_done)]
```

---

## 9. CLI

```bash
# Structural stages
python main.py project    [--force]
python main.py assets     [--force]
python main.py breakdown  [--force]

# Filling stages — interactive clip selection
python main.py shots      [--all-clips] [--limit N]
python main.py keyframes  [--all-clips] [--limit N]
python main.py motion     [--all-clips] [--limit N]

# Everything (respects clip selection at stages 4–6)
python main.py all        [--force]
```

| Flag | Effect |
|---|---|
| `--force` | Delete + regenerate structural stage output (1, 2, 3) |
| `--all-clips` | Skip clip prompt; process all pending clips |
| `--limit N` | With `--all-clips`; process first N pending |

---

## 10. Resolved Decisions (31 Questions)

### A. Inputs & Sheet Handling

| # | Question | Decision |
|---|---|---|
| A1 | Sheet filename format | `input/char_XX_sheet.png` — `.png` only |
| A2 | Multiple sheets per character | One sheet per character; extra files ignored |
| A3 | Sheet size/resolution check | None |
| A4 | No sheets but characters present | **Hard fail** with clear error |

### B. Structural Stages (1, 2, 3)

| # | Question | Decision |
|---|---|---|
| B1 | `--force` on filling stages | Not allowed; clip selection handles reprocessing |
| B2 | Corrupt structural output | Fail with clear error; user must `--force` |
| B3 | Clip budget enforcement | Soft target (±50% tolerance); abort only if wildly off |
| B4 | Zero clips from Stage 3 | Abort with error; requires `--force` |

### C. Filling Stages (4, 5, 6)

| # | Question | Decision |
|---|---|---|
| C1 | Multi-character clips | **Single-character mode (v1).** Use `characters_present[0]`; warn if >1. Future: per-character state lists |
| C2 | Zero-character clips | Handle via `pose_id="pose_none"`, `character_id=""` |
| C3 | Reuse across scenes | **No.** Same-scene only |
| C4 | Reuse across segments (same scene) | **Yes**, allowed |
| C5 | Seeds | **Deterministic** via `sha256(clip_id + role + "qwen")[:4]` / `sha256(clip_id + "ltx")[:4]` |
| C6 | LLM retry policy | Retry up to 3× with rephrase; mark clip failed after 3; continue |

### D. Concurrency & Error Handling

| # | Question | Decision |
|---|---|---|
| D1 | `max_concurrency` default | **1** (overridable in config) |
| D2 | LLM connection errors | Retry with exponential backoff (1s, 2s, 4s); then mark clip failed, continue |
| D3 | Per-call timeout | **300 seconds** |
| D4 | `reasoning_effort` propagation | `None` for all stages (model default) |

### E. Output Format & Durability

| # | Question | Decision |
|---|---|---|
| E1 | Atomic writes | **Yes** — `output/tmp/<name>.<pid>.tmp` → `os.replace()` |
| E2 | Timestamp/provenance fields | **None** — file `mtime` suffices |
| E3 | JSON key ordering | Pydantic field order preserved; `indent=2`, `ensure_ascii=False` |
| E4 | `6_final.json` shape | **Nested** — full merged tree |
| E5 | Log file | **None** — stdout only via `print()` |
| E6 | `--quiet` flag | Not in v1; `--all-clips` suffices |

### F. Clip Selection UX

| # | Question | Decision |
|---|---|---|
| F1 | Non-TTY environment | Auto-fallback to `--all-clips` behavior |
| F2 | `--dry-run` flag | Not in v1 |
| F3 | Scene confirmation pause | Skip — per-clip save makes Ctrl+C safe |

### G. Prerequisites, Testing, Extras

| # | Question | Decision |
|---|---|---|
| G1 | Stage prerequisites | Checked explicitly — fail with clear message if missing |
| G2 | `run_all` clip selection | Interactive (respects `interactive_clip_selection`) |
| G3 | Sample fixture | `input/story.txt.example` provided for smoke-testing |
| G4 | pytest | Minimal — `_parse_clip_csv` and `discover_character_sheets` only |
| G5 | Post-Stage-6 validation script | Not in v1 |
| G6 | Default model name | `qwen3.5-2b-aggressive` |

---

## 11. Configuration (`PipelineConfig`)

```python
@dataclass
class PipelineConfig:
    # LLM
    lm_studio_url: str = "http://localhost:1234/v1"
    model_name:    str = "qwen3.5-2b-aggressive"
    api_key:       str = "1234"
    reasoning_effort: str | None = "low"

    # Concurrency
    max_concurrency: int = 1

    # Paths
    input_dir:  str = "input"
    output_dir: str = "output"

    # Filenames
    story_filename:               str = "story.txt"
    project_filename:             str = "1_project.json"
    assets_filename:              str = "2_assets.json"
    scenes_filename:              str = "3_scenes.json"
    scenes_shots_filename:        str = "4_scenes-shots.json"
    scenes_keyframes_filename:    str = "5_scenes-keyframes.json"
    final_filename:               str = "6_final.json"

    # Per-stage temperatures
    project_temperature:    float = 0.4
    assets_temperature:     float = 0.7
    breakdown_temperature:  float = 0.5
    shots_temperature:      float = 0.4
    keyframes_temperature:  float = 0.5
    motion_temperature:     float = 0.6

    # Selection & resilience
    interactive_clip_selection: bool = True
    default_timeout:            int  = 300
    llm_max_retries:            int  = 3
```

All file paths exposed as `@property` returning `Path` objects (`story_path`, `project_path`, etc.).

---

## 12. Edge Cases

| Case | Behavior |
|---|---|
| Story missing or empty | Fail-fast at every stage entry via `validate_inputs()` |
| `input/` folder missing | Caught via `story_path.exists() == False` |
| Character sheet missing for a character | Hard fail at Stage 2 (per A4) |
| Multiple character sheets for different characters | Warning only; single-character mode active |
| Extra PNG in `input/` not matching pattern | Silently ignored |
| Both `.png` and `.jpg` | `.png` only accepted (per A1); `.jpg` ignored |
| Corrupt `N_*.json` | Fail with clear error; user must `--force` |
| Clip fails after 3 retries | Logged, skipped, remains `[    ]` |
| Ctrl+C mid-run | Atomic writes ensure no corruption; saved clips survive |
| User selects `[DONE]` clip | Reprocessed (no confirmation) |
| Malformed CSV | Caught, error printed, re-prompted |
| Non-TTY stdin | Auto-fallback to `--all-clips` behavior |
| `run_all` and stage 1 fails | `run_all` aborts, later stages not attempted |

---

## 13. Deferred to Future Versions

The following were explicitly deferred during design. They are **not** in v1:

| Item | Reference | Notes |
|---|---|---|
| Multi-character per-clip state lists | C1 | Single-character mode for now |
| `--dry-run` flag | F2 | Clip selection with `none` serves as preview |
| `--quiet` flag | E6 | `--all-clips` suffices |
| Scene-level confirmation pause | F3 | Per-clip save makes Ctrl+C safe |
| Full pytest suite | G4 | Two helper functions tested manually |
| Post-Stage-6 validation script | G5 | Downstream ComfyUI catches issues |
| `--force` on filling stages | B1 | Clip selection handles reprocessing |
| Timestamps in output JSON | E2 | File `mtime` suffices |
| Pipeline log file | E5 | stdout only |
| Additional style presets | — | Four provided; extendable via `STYLE_PRESETS` |

---

## 14. Deliverables

Upon approval, the implementation consists of:

| File | Purpose |
|---|---|
| `__init__.py` | Package exports |
| `main.py` | CLI entry point |
| `config.py` | `PipelineConfig` dataclass |
| `llm_client.py` | LangChain model wrapper |
| `models.py` | Pydantic schemas (all 6 stages) |
| `storage.py` | Filesystem I/O + `iter_clips` + validation |
| `templates.py` | RISCOR prompts + `VOCABULARIES` + `STYLE_PRESETS` |
| `pipeline.py` | `StoryPipeline` facade |
| `stages/__init__.py` | Stage exports |
| `stages/base.py` | `PipelineStage` ABC + clip selection helpers |
| `stages/stage1_project.py` | Stage 1 |
| `stages/stage2_assets.py` | Stage 2 |
| `stages/stage3_breakdown.py` | Stage 3 |
| `stages/stage4_shots.py` | Stage 4 |
| `stages/stage5_keyframes.py` | Stage 5 |
| `stages/stage6_motion.py` | Stage 6 |
| `README.md` | User-facing contract + CLI reference |
| `input/story.txt.example` | Sample fixture for smoke-testing |

---

## 15. Quick Reference

**First run:**
```bash
mkdir -p input
cp input/story.txt.example input/story.txt      # or write your own
cp /path/to/sheet.png input/char_01_sheet.png
python main.py project
python main.py assets
python main.py breakdown
python main.py shots
python main.py keyframes
python main.py motion
```

**Resume after interrupt:** Re-run the same command. Stages 1–3 skip if output exists. Stages 4–6 load and continue from last-saved clip.

**Regenerate a structural stage:** `python main.py <stage> --force`.

**Reprocess a specific clip:** Run the filling stage, enter its number in the CSV prompt.

**Automation:** Add `--all-clips` to any filling stage to skip the prompt.

---

**End of document.**