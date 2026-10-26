Create a ticket 

## Project Plan: Pure-LLM Orchestrated Story-to-Video Pipeline

### Phase 0: Foundation — Contracts & Environment

| # | Task | Description | LLM Role | Dependencies | Deliverable | Exit Criteria |
|---|---|---|---|---|---|---|
| 0.1 | **Finalize JSON Schema v2.0** | Lock the hierarchy (`project → scene → segment → clip → keyframe → video`), vocabularies, and templates from your design. | None (human decision) | Your existing `schema_version 2.0` | `schema/master.json` frozen | Schema validated against JSON Schema Draft 2020-12. |
| 0.2 | **Define Pydantic models** | Mirror every JSON object as a Pydantic model with enum constraints for all vocabulary fields. | None (code) | 0.1 | `models/` package | `model_validate()` passes on your worked example. |
| 0.3 | **Build the validator layer** | Implement rules from `constraints`: `size_multiple=32`, `num_frames_rule=8n+1`, `framing_locked_within_clip`, `max_pose_changes_per_keyframe`. | None (code) | 0.2 | `validators/` package | Rejects a deliberately broken clip with specific error messages. |
| 0.4 | **Stand up ComfyUI API client** | REST + WebSocket client that can queue Qwen Edit and LTX workflows and poll for completion. | None (code) | ComfyUI running | `comfy/client.py` | Successfully queues a dummy workflow and retrieves output path. |
| 0.5 | **Pin Qwen 2B model** | Choose the exact Qwen 2B variant (Instruct vs. base) and confirm it supports structured JSON output via a grammar or `--json-schema` equivalent. | None (infrastructure) | Model downloaded | Model card + inference script | Model returns valid JSON for a 3-field schema on 10/10 trials. |

---

### Phase 1: Environment Plates (Stage 0 — No Character Yet)

| # | Task | Description | LLM Role | Dependencies | Deliverable | Exit Criteria |
|---|---|---|---|---|---|---|
| 1.1 | **LLM generates plate descriptions** | Given `project.style_bible` and a one-line location note, the LLM fills `environment_id`, `name`, `description`, and `landmarks[]` for every unique location × time-of-day. | **Primary creative** — invents the geography and landmark layout. | 0.2 | `assets/environments` populated | Every planned scene has a valid `environment_ref`. |
| 1.2 | **Code renders `plate_t2i` prompts** | Concatenate style + environment text + time + light + view into the `plate_t2i` template. | None (code) | 1.1, 0.1 | `rendered_prompt` for each plate | All prompts are non-empty and use closed vocabulary values. |
| 1.3 | **Generate plates via T2I** | Send each plate prompt to ComfyUI (Flux/SDXL) and save output. | None (ComfyUI) | 1.2 | `plates[].output_path` set | Visual inspection confirms landmark positions match `landmarks[]`. |

---

### Phase 2: LLM Story Decomposition (Scenes → Segments → Clips)

| # | Task | Description | LLM Role | Dependencies | Deliverable | Exit Criteria |
|---|---|---|---|---|---|---|
| 2.1 | **Scene outline call** | Feed the story text (and `style_bible`) to the 2B model. Ask for a flat JSON array of `scenes[]` with `scene_id`, `name`, `summary`, `time_of_day`, `weather`, `light`, `ambience`. | **Primary creative** — decides pacing, location changes, and time progression. | 0.2, Phase 1 | `scenes/s01.json`, `s02.json`, … | Total runtime estimate (scenes × avg clips × 5 s) is within ±10% of `target_runtime_min`. |
| 2.2 | **Segment split call** | For each scene, ask the LLM to split `summary` into `segments[]` with `beat_summary` and `emotional_arc`. | **Primary creative** — identifies story beats. | 2.1 | `segments[]` populated | Every segment maps to exactly one story beat; no orphan or overlapping beats. |
| 2.3 | **Clip breakdown call** | For each segment, ask the LLM to produce `clips[]` with `story_action` (one sentence) and `continuity.transition_in` (from the vocabulary). | **Primary creative** — decides what happens in each 5-second unit. | 2.2 | `clips[]` with `story_action` | Total clips per scene is plausible (≈ runtime_min × 12). |
| 2.4 | **Validate decomposition** | Check that every `transition_in` is a valid vocabulary value and that `story_action` length is within token budget. | **LLM self-check** (optional second pass) or code | 2.3, 0.3 | Validated scene files | Zero vocabulary violations; retry any failing clip automatically. |

**Reference pattern:** This mirrors **PenShot**, which uses LangGraph agents for script parsing → storyboard generation → video splitting, with a Continuity Guardian agent that ensures cross-shot consistency.

---

### Phase 3: LLM Shot & Keyframe State Generation

| # | Task | Description | LLM Role | Dependencies | Deliverable | Exit Criteria |
|---|---|---|---|---|---|---|
| 3.1 | **Shot selection call** | For each clip, ask the LLM to fill `shot.shot_type`, `camera_angle`, `lens_mm`, `camera_movement`, and `focus_point` from the closed vocabularies. | **Primary creative** — chooses cinematic grammar. | 2.3 | `shot` filled | No `camera_movement` other than `static` or `slow_*` (your `camera_default` constraint). |
| 3.2 | **First keyframe state call** | Given `story_action`, `shot`, wardrobe, and pose library, ask the LLM to fill `keyframes.first.state` (pose, position, facing, expression, condition, prop). | **Primary creative** — decides blocking and pose. | 3.1 | `first.state` populated | `pose_id` exists in `pose_library`; `wardrobe_state` exists in wardrobe states. |
| 3.3 | **Middle & last delta call** | Ask the LLM to fill **only the delta fields** for middle and last (`pose_id`, `wardrobe_state`, `expression`, `condition_wetness`) — **not the full state**. | **Primary creative** — defines the motion arc. | 3.2 | `middle_delta`, `last_delta` | Delta changes ≤ `max_pose_changes_per_keyframe` (2). |
| 3.4 | **Code merges delta → full state** | Merge each delta onto the previous keyframe's full state to produce resolved `middle.state` and `last.state`. | None (code) | 3.3 | Resolved 3-state tuple | All fields present; no unresolved placeholders. |
| 3.5 | **LLM motion prompt call** | Ask the LLM to fill `video.motion.subject_motion`, `secondary_motion`, `ambient_motion`, and `audio.*` for the clip. | **Primary creative** — describes the 5-second motion. | 3.4 | `video.motion` filled | `subject_motion` is consistent with the keyframe deltas from 3.3. |

**Design rationale:** Splitting the keyframe call into **first-state** + **delta-only** reduces the 2B model's output surface from ~12 fields to ~4 per subsequent keyframe, which is critical for reliability at the 2B scale. This aligns with the **AI ONE DIRECTOR** pattern of structured scenario planning before generation.

---

### Phase 4: Deterministic Rendering & QA

| # | Task | Description | LLM Role | Dependencies | Deliverable | Exit Criteria |
|---|---|---|---|---|---|---|
| 4.1 | **Render all `qwen_*` prompts** | Code concatenates resolved state + templates into `rendered_prompt` for every keyframe. | None (code) | Phase 3 | `rendered_prompt` non-null | No empty slots; identity lock injected verbatim. |
| 4.2 | **Generate keyframes in order** | For each clip: `first` (plate+sheet or reuse), then `middle` (chain from first), then `last` (chain from middle). | None (Qwen Edit) | 4.1, Phase 1 | `image_gen.output_path` set | All three keyframes exist for every clip. |
| 4.3 | **QA keyframes (identity + framing)** | Score face similarity against character sheet; verify framing matches `shot` (subject height, position). | **LLM vision model** (optional) or CLIP/InsightFace scorer | 4.2 | `qa_result` populated | `identity_score ≥ qa_policy.identity_score_min` (0.8). |
| 4.4 | **Retry / reset-to-plate** | On QA failure, retry with a new seed up to `max_attempts`. After `reset_chain_to_plate_after_failures` consecutive failures, re-anchor to `plate + sheet`. | None (code + ComfyUI) | 4.3 | Accepted keyframe | Every clip has 3 accepted keyframes. |
| 4.5 | **Periodic re-anchoring** | Every **N clips** (e.g., 5) or at the start of every **new scene**, force `reuse_from: null` and regenerate from the plate. | None (code) | 4.2 | Chain reset applied | Drift does not compound beyond the re-anchor interval. |

**Critical note:** The **re-anchoring rule** is not optional. Open-loop chaining of Qwen edits accumulates drift; the plate is your ground-truth anchor. This is the same logic that **PenShot's Continuity Guardian** uses multi-level memory to enforce.

---

### Phase 5: Video Generation & Assembly

| # | Task | Description | LLM Role | Dependencies | Deliverable | Exit Criteria |
|---|---|---|---|---|---|---|
| 5.1 | **Render `ltx_motion` prompts** | Code concatenates `shot` + `motion` + `style_bible` into the final LTX prompt. | None (code) | Phase 3, 4.1 | `video.rendered_prompt` | No empty motion fields. |
| 5.2 | **Run LTX FML per clip** | Send `first`, `middle`, `last` keyframes + prompt to LTX. Use **Q4 GGUF quant** for 12 GB VRAM. | None (LTX) | 4.4, 5.1 | `video.output_path` | Output is 121 frames (≈5 s @ 24 fps). |
| 5.3 | **Validate clip duration & frame rule** | Confirm every output is exactly `num_frames_rule` (8n+1) and duration ≈ 5 s. | None (code) | 5.2 | Validated video files | All clips pass duration check. |
| 5.4 | **Concatenate final film** | Use `ffmpeg` to stitch all clips in scene → segment → clip order. | None (code) | 5.3 | `final_film.mp4` | Total runtime matches `target_runtime_min` ±10%. |

---

### Phase 6: Scale, Monitor & Iterate

| # | Task | Description | LLM Role | Dependencies | Deliverable | Exit Criteria |
|---|---|---|---|---|---|---|
| 6.1 | **Per-call prompt contracts** | For each of the 5 LLM calls (scene outline, segment split, clip breakdown, shot+state, motion), write a few-shot example and a strict output schema. | **LLM few-shot examples** generated by human or larger model | Phase 3 | `prompts/` directory | 2B model produces valid JSON on 20/20 dry runs per call. |
| 6.2 | **Automated retry loop** | Wrap each LLM call in a validator → retry loop with a max of 3 attempts per call. | None (code) | 6.1, 0.3 | `llm_runner.py` | Failed calls are logged and retried without human intervention. |
| 6.3 | **Run a full scene end-to-end** | Pick one scene (≈5–10 clips) and run the entire pipeline: plates → LLM decomposition → keyframes → LTX → concat. | All of the above | All phases | Scene MP4 | Scene completes without OOM on 12 GB GPU. |
| 6.4 | **Measure & tune** | Track per-call LLM latency, JSON validity rate, identity score, and VRAM peak. Adjust `max_pose_changes_per_keyframe`, re-anchor interval, and LTX quant if needed. | **LLM failure analysis** (optional) | 6.3 | Metrics dashboard | Metrics stable across 3 consecutive scene runs. |
| 6.5 | **Scale to full film** | Run all scenes, monitor for drift, and manually review every 5th clip. | **LLM-assisted QA** (vision scoring) | 6.4 | Final 15–30 min film | Film passes spot-check review. |