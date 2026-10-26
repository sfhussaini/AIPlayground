# Story → Video Pipeline

Plans a movie from a story: one 5-second clip at a time. Qwen Image Edit makes
the keyframes, LTX makes the clips, FFmpeg joins them (outside this program).

## Inputs (./input/)
- story.txt                       required (up to about 1500 words)
- story_fragmented.json           produced by `python main.py breakdown`
                                  (LLM + human review; see below)
- char_01_sheet.png               required: neutral background, full body
- char_01_sheet.json              produced by `python main.py characters`
                                  (Vision LLM + human review)
- character.json                  legacy single-file fallback; only read if
                                  no per-sheet JSONs exist

Both `story_fragmented.json` and `char_XX_sheet.json` are **LLM-produced,
human-reviewed, and gate a downstream stage**. They live in `input/` (not
`output/`) for that reason.

## Reference data (./reference/)
- vocabularies.json, light_presets.json, poses.json, shot_lens.json,
  labels.json, condition_text.json, negatives.json, style_presets.json,
  banned_phrases.json, prompt_templates.json, color_grades.json

  Read-only at rest. `poses.json` grows when Stage 2 accepts a new pose.
  Sibling of input/ and output/ so wiping those never loses it.

## Outputs (./output/)
- 1_project.json            style, negatives
- 2_assets.json             character, places, props, pose library
- 3_scenes.json             scenes → clips (schema 3.0), no segments
- 4_scenes-shots.json       + camera per clip
- 5_scenes-keyframes.json   + first/middle/last states per clip
- 6_scenes-motion.json      + motion and audio per clip
- 7_qwen-prompts.json       + a complete Qwen prompt per keyframe
- 8_ltx-prompts.json        + an LTX prompt per clip (final artifact)
- 9_qwen-images.json        flat list of Qwen jobs for Comfy

## Commands
python main.py characters  [--force]     (Vision extract per-sheet JSONs)
python main.py project     [--force]
python main.py assets      [--force]     (may prompt for new poses)
python main.py breakdown   [--force] [--re-fragment] [--refine ID]
python main.py shots | keyframes | motion   [--all-clips] [--limit N]
python main.py qwen-prompts [--limit N]
python main.py ltx-prompts  [--limit N]
python main.py qwen-export
python main.py validate
python main.py check-character
python main.py check-fragmented
python main.py all [--force]

## Fragmentation workflow (Stage 3)

`3_scenes.json` is built by two stages:

- **Stage 3a** (LLM) reads the *whole* story in a single call and produces
  `input/story_fragmented.json` with `review.reviewed = false`.
- **Stage 3b** (code) refuses to run until a human approves the fragments.

Workflow:

1. `python main.py breakdown` (first run)
   → Stage 3a writes `input/story_fragmented.json`.
   → Stage 3b refuses until `review.reviewed = true`.
2. Inspect / edit `input/story_fragmented.json`.
   Structure: sequences → scenes → clips. No `shot` level — a clip *is*
   the leaf (one camera setup, one LTX render, one output video).
3. Set `review.reviewed = true`.
4. `python main.py breakdown` again
   → Stage 3b builds `output/3_scenes.json` (schema 3.0).

Re-run semantics:

- `--force`        → rebuild `output/3_scenes.json` from the existing
                     fragments. Never touches `input/story_fragmented.json`.
- `--re-fragment`  → delete fragments and redo the whole fragmentation.
                     Destroys edits. Cheap now (single LLM call).
- `--refine <id>`  → redo one **scene** (`sq_01.sc_02`) or **clip**
                     (`sq_01.sc_02.c03`). Sequence-level refine is not
                     supported — use `--re-fragment` for that. Any refine
                     resets `review.reviewed` to false and appends a line
                     to `review.notes`; re-approve before 3b runs.

If `input/story.txt` changes after fragments were written, a plain
`breakdown` refuses to run (sha1 mismatch). Use `--re-fragment` or restore
the story.

## Notes
- The LLM does all language work. Code only handles files, ids, seeds, fixed
  libraries and string matching. Ids (`sq_NN`, `sq_NN.sc_NN`, `sq_NN.sc_NN.cNN`)
  are assigned by code; the fragmenter LLM never sees or emits them.
- Every LLM call stays under 500 words except the fragmenter (whole story,
  by design) and refine calls (whole story + current state, by design).
- Re-running an earlier stage resets only the downstream clips whose inputs changed.
- Progressive dimming / zoom drift in a reuse chain: fixed by suppressing
  unchanged `Lighting:` / `Framing:` sentences in Stage 7. A keyframe whose
  reference is a previous rendered frame only re-states these when they
  change; a fresh-start keyframe (reference = character sheet) always
  states both. If drift persists at scene or shot boundaries, extend
  `_DELTA_FIELDS` in stage7_qwen_prompts.py with `"position_text"`.
- Lighting controls (Stage 3b, config.py):
    force_time_of_day      force every scene to one time (e.g. "morning")
    clamp_dark_lighting    soften pre_dawn/dusk/night one notch brighter
    dark_time_warn_threshold  warn when too many clips use dark times
- `require_fragmentation_review = True` by default: Stage 3b refuses to run
  on unreviewed fragments. Set to `False` in `config.py` for unattended runs.

## Wardrobe states

Wardrobe state variants are **per-story**. They live only in
`output/2_assets.json::assets.characters[].wardrobe[].states`.

Where they come from:

- **Stage 1.5** writes `{"default": "worn as shown in the reference sheet"}`
  into `input/char_XX_sheet.json`. Human approves the sheet JSON.
- **Stage 2** reads the fragmenter's clip actions and makes one LLM call
  per wardrobe: *"propose extra outfit states this wardrobe would show in
  this story."* Proposals are deduped against the current states, capped so
  the total stays ≤ 5, and shown in a batch review:

    [a] accept all  [r] review one by one  [s] skip all  [q] quit

  Accepted states are written into `output/2_assets.json`.
- **Stage 5** picks a `wardrobe_state` from a `Literal[...]` enum built
  from `wardrobe[0].states.keys()`, alongside pose / facing / expression.

There is **no `reference/states.json`**. The per-story nature of the data
was the point: story 2 (a city story) should never see story 1's `muddy_legs`.

Config gates:
- `interactive_state_review = True` (default) — propose + review per wardrobe.
  Set to `False` for unattended runs; every wardrobe keeps only `default`.
- `state_temperature` — temperature for the state-proposal LLM call.

## Reference data (./reference/)
- vocabularies.json, light_presets.json, poses.json, shot_lens.json,
  labels.json, negatives.json, style_presets.json, banned_phrases.json,
  prompt_templates.json, color_grades.json

  All story-independent: closed enums, labels, presets, prompts, and the
  growing universal pose library. `poses.json` grows when Stage 2 accepts a
  new pose. Environments, props, and wardrobe states are per-story and live
  only in `output/2_assets.json`.