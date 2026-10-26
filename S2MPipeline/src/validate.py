"""validate.py - code-only checks. No LLM.

    python main.py validate          (every output that exists)
    python main.py check-character   (per-sheet character JSONs, plus legacy)
    python main.py check-fragmented  (input/story_fragmented.json only)
    python main.py check-states      (wardrobe_state end-to-end consistency)

Fragments-first, no 2_assets.json:
  - Per-story libraries (environments, wardrobe_states, props) are authored
    by the fragmenter into input/story_fragmented.json, then copied with
    sentinels ('default', 'none') into output/3_scenes.json.
  - Character data comes from input/char_XX_sheet.json.
  - Pose library stays in reference/poses.json (growing).

Check split by concern:
  check_fragmented_script : fragment shape + library shape + scene->env refs
  check_scenes            : 3_scenes.json shape (libraries + characters + clips)
  check_keyframes         : per-keyframe state correctness
  check_states            : library + keyframe-level state consistency
  check_prompts           : rendered_prompt QA
"""
import re
from typing import Iterator, List, Optional, Tuple

from pydantic import ValidationError

from models import CharacterFile
from reference import NO_POSE, POSES
from templates import (
    BANNED_PHRASES, LIGHT_PRESETS, LLM_SYSTEM_PROMPTS, SHOT_LENS, VOCABULARIES,
)

Issue = Tuple[str, str]   # ("ERROR" | "WARN", message)

_CLOTHING = ("dress", "shirt", "wearing", "saree", "sari", "clothes", "pants",
             "jeans", "kurta", "skirt", "blouse")
_SNAKE = re.compile(r"\b[a-z0-9]+_[a-z0-9_]+\b")
_FACE = {"oval": "an oval face", "round": "a round face",
         "square": "a square face", "heart": "a heart-shaped face",
         "long": "a long face"}
_LEN = {"short": "short", "chin_length": "chin-length",
        "shoulder_length": "shoulder-length", "mid_back": "mid-back-length",
        "waist_length": "waist-length"}
_UPDO = {"braided": "in a braid", "bun": "in a bun",
         "ponytail": "in a ponytail", "tied_back": "tied back"}
_PERSON_WORDS = frozenset({
    "she", "he", "her", "his", "him", "they", "them",
    "woman", "man", "girl", "boy", "person", "character",
    "mother", "father", "sister", "brother", "child",
})

# pose_id valid set: all library poses + pose_none (the reserved non-pose).
# 'pose_new' is a sentinel only valid during Stage 5 generation; it must never
# appear in a saved file (Stage 5's _resolve_pose_proposals replaces it).
_POSE_IDS = {p["pose_id"] for p in POSES} | {NO_POSE["pose_id"]}


def _w(s: str) -> str:
    return s.replace("_", " ")


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip(" .")


def _action_has_person(action: str) -> bool:
    words = re.findall(r"\w+", (action or "").lower())
    return any(w in _PERSON_WORDS for w in words)


def _env_slug(name: str) -> str:
    """Same slug rule as Stage 3a and 3b - kept local to avoid import cycles."""
    s = re.sub(r"^(the|a|an|her|his|their|its)\s+",
               "", (name or "").lower().strip())
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "place"


# ---------------------------------------------------------------------------
# Identity lock helpers (used by stage 3b / character sheet validation)
# ---------------------------------------------------------------------------
def compose_identity_lock(ch: dict) -> str:
    i, h, subject = ch["identity"], ch["identity"]["hair"], ch["subject"]
    color = _w(h["color"])
    if h["length"] == "bald":
        hair = "a bald head"
    elif h["style"] in _UPDO:
        hair = f"{_LEN[h['length']]} {color} hair {_UPDO[h['style']]}"
    elif h["style"] == "other":
        hair = f"{_LEN[h['length']]} {color} hair"
    else:
        hair = f"{_LEN[h['length']]} {color} {h['style']} hair"
    feats = [_FACE[i["face_shape"]], f"{_w(i['skin_tone'])} skin",
             f"{_w(i['eye_color'])} eyes", hair]
    if i["facial_hair"] != "none":
        feats.append(f"a {_w(i['facial_hair'])}")
    s = f"the {subject} has {', '.join(feats[:-1])} and {feats[-1]}"
    marks = i.get("facial_marks") or []
    if marks:
        s += f", with {' and '.join(marks)}"
    return s


def lock_is_valid(lock: str, ch: dict) -> bool:
    low = (lock or "").lower().strip()
    if not (40 <= len(low) <= 300):
        return False
    if not low.startswith(f"the {ch['subject']}"):
        return False
    if any(w in low for w in _CLOTHING):
        return False
    i, h = ch["identity"], ch["identity"]["hair"]
    if _w(i["skin_tone"]) not in low:
        return False
    if h["length"] != "bald" and _w(h["color"]) not in low:
        return False
    return low.count(",") <= 4


# ---------------------------------------------------------------------------
# Character validation (per sheet JSON + legacy)
# ---------------------------------------------------------------------------
def _check_character_entry(entry: dict, storage) -> List[Issue]:
    issues: List[Issue] = []
    cfg = storage._config
    if entry["sheet_file"] != f"{entry['character_id']}_sheet.png":
        issues.append(("ERROR",
                       f"sheet_file must be {entry['character_id']}_sheet.png"))
    if not (cfg.input_path / entry["sheet_file"]).exists():
        issues.append(("ERROR",
                       f"Character sheet not found: "
                       f"{cfg.input_path / entry['sheet_file']}"))
    if not lock_is_valid(entry["identity_lock"], entry):
        issues.append(("WARN",
                       "identity_lock is not a clean sentence; "
                       "it will be regenerated."))
    sc = entry["sheet_check"]
    if not sc["full_body"]:
        issues.append(("WARN", "Sheet is not full-body; poses may drift."))
    if not sc["plain_background"]:
        issues.append(("WARN",
                       "Sheet background is not plain; scene bleed is likely."))
    for msg in sc["issues"]:
        issues.append(("WARN", f"sheet issue: {msg}"))
    if not entry["review"]["reviewed"]:
        level = "ERROR" if cfg.require_character_review else "WARN"
        issues.append((level,
                       "review.reviewed is false. Check the file, then set "
                       "it to true."))
    for f in entry["review"]["uncertain_fields"]:
        issues.append(("WARN", f"Vision LLM was unsure about: {f}"))
    return issues


def load_character_file(storage) -> Tuple[Optional[dict], List[Issue]]:
    """Legacy: load a single character from input/character.json."""
    issues: List[Issue] = []
    cfg = storage._config
    if not storage.character_exists():
        issues.append(("ERROR",
                       f"Missing {cfg.character_path}. Fill it with a Vision "
                       f"LLM using character.schema.json, or run "
                       f"'python main.py characters'."))
        return None, issues
    try:
        raw = storage.read_character_raw()
    except Exception as exc:
        issues.append(("ERROR",
                       f"{cfg.character_path} is not valid JSON: {exc}"))
        return None, issues
    try:
        entry = CharacterFile.model_validate(raw).characters[0].model_dump()
    except ValidationError as exc:
        for e in exc.errors():
            loc = ".".join(str(p) for p in e["loc"])
            issues.append(("ERROR", f"character.json {loc}: {e['msg']}"))
        return None, issues
    issues.extend(_check_character_entry(entry, storage))
    return entry, issues


def load_all_characters(storage) -> Tuple[List[dict], List[Issue]]:
    """Prefer per-sheet JSONs (input/char_XX_sheet.json). Fall back to
    input/character.json for backward compatibility."""
    issues: List[Issue] = []
    entries: List[dict] = []

    per_sheet = list(storage.iter_character_sheet_jsons())
    if per_sheet:
        for char_id, raw in per_sheet:
            if raw is None:
                issues.append(("ERROR",
                               f"input/{char_id}_sheet.json is empty or "
                               f"invalid JSON."))
                continue
            try:
                doc = CharacterFile.model_validate(raw)
                for e in doc.characters:
                    entries.append(e.model_dump())
            except ValidationError as exc:
                for e in exc.errors():
                    loc = ".".join(str(p) for p in e["loc"])
                    issues.append(("ERROR",
                                   f"{char_id}_sheet.json {loc}: {e['msg']}"))
        for entry in entries:
            issues.extend(_check_character_entry(entry, storage))
        return entries, issues

    entry, legacy_issues = load_character_file(storage)
    if entry is not None:
        entries.append(entry)
    issues.extend(legacy_issues)
    return entries, issues


# ---------------------------------------------------------------------------
# Template check (RISCOR)
# ---------------------------------------------------------------------------
def check_templates() -> List[Issue]:
    out: List[Issue] = []
    headers = ["ROLE:", "INSTRUCTION:", "STEPS:", "CONTEXT:", "OUTPUT:", "RULES:"]
    for name, text in LLM_SYSTEM_PROMPTS.items():
        pos = [text.find(h) for h in headers]
        if -1 in pos:
            out.append(("ERROR",
                        f"{name}: missing header {headers[pos.index(-1)]}"))
        elif pos != sorted(pos):
            out.append(("ERROR", f"{name}: RISCOR headers are out of order"))
        n = len(text.split())
        if n > 130:
            out.append(("ERROR", f"{name}: {n} words (limit 130)"))
    return out


# ---------------------------------------------------------------------------
# Fragments (input/story_fragmented.json) - fragments-first, no 2_assets
# ---------------------------------------------------------------------------
def _check_libraries(doc: dict) -> List[Issue]:
    out: List[Issue] = []

    envs = doc.get("environments") or []
    names = [(e.get("name") or "").strip().lower() for e in envs]
    if len(names) != len(set(names)):
        out.append(("ERROR", "duplicate environment names in 'environments'"))

    state_ids: List[str] = []
    for s in doc.get("wardrobe_states") or []:
        sid = s.get("state_id", "")
        if not re.match(r"^[a-z][a-z_]*$", sid):
            out.append(("ERROR",
                        f"wardrobe_states: invalid state_id {sid!r}"))
        state_ids.append(sid)
    if len(state_ids) != len(set(state_ids)):
        out.append(("ERROR", "duplicate state_ids in 'wardrobe_states'"))

    prop_ids: List[str] = []
    for p in doc.get("props") or []:
        pid = p.get("prop_id", "")
        if not re.match(r"^prop_[a-z][a-z0-9_]*$", pid):
            out.append(("ERROR", f"props: invalid prop_id {pid!r}"))
        prop_ids.append(pid)
    if len(prop_ids) != len(set(prop_ids)):
        out.append(("ERROR", "duplicate prop_ids in 'props'"))

    return out


def check_fragmented_script(storage) -> List[Issue]:
    issues: List[Issue] = []
    cfg = storage._config

    if not storage.fragmented_exists():
        return [("ERROR", f"Missing {cfg.fragmented_path}. "
                          f"Run: python main.py breakdown")]

    try:
        doc = storage.read_fragmented()
    except Exception as exc:
        return [("ERROR", f"{cfg.fragmented_path} is not valid JSON: {exc}")]

    reviewed = bool((doc.get("review") or {}).get("reviewed"))
    if not reviewed:
        level = "ERROR" if cfg.require_fragmentation_review else "WARN"
        issues.append((level,
                       "review.reviewed is false. Set to true after checking."))

    source_sha1 = (doc.get("source") or {}).get("sha1")
    current_sha1 = storage.story_sha1()
    if source_sha1 and current_sha1 and source_sha1 != current_sha1:
        issues.append(("WARN",
                       "story.txt changed since fragments were written; "
                       "run: python main.py breakdown --re-fragment"))

    issues.extend(_check_libraries(doc))

    # Scene environment_name must reference a listed environment.
    env_slugs = {_env_slug((e.get("name") or ""))
                 for e in doc.get("environments") or []}

    # Character IDs only cross-checked when sheets exist.
    char_ids: set = set()
    for char_id, raw in storage.iter_character_sheet_jsons():
        if raw is None:
            continue
        try:
            for c in raw.get("characters", []):
                if c.get("character_id"):
                    char_ids.add(c["character_id"])
        except Exception:
            continue
    if not char_ids and storage.character_exists():
        try:
            raw = storage.read_character_raw()
            for c in raw.get("characters", []):
                if c.get("character_id"):
                    char_ids.add(c["character_id"])
        except Exception:
            pass

    time_vocab = set(VOCABULARIES["time_of_day"])

    total_clips = 0
    for seq in doc.get("sequences", []):
        for sc in seq.get("scenes", []):
            sc_id = sc.get("scene_id", "?")
            env_name = (sc.get("environment_name") or "").strip()
            if not env_name:
                issues.append(("ERROR", f"{sc_id}: environment_name is empty"))
            elif _env_slug(env_name) not in env_slugs:
                issues.append(("ERROR",
                               f"{sc_id}: environment_name {env_name!r} "
                               f"not in 'environments' list"))
            tod = sc.get("time_of_day")
            if tod not in time_vocab:
                issues.append(("ERROR",
                               f"{sc_id}: time_of_day {tod!r} not in "
                               f"vocabulary"))
            for c in sc.get("characters_present", []):
                if char_ids and c not in char_ids:
                    issues.append(("ERROR",
                                   f"{sc_id}: unknown character {c}"))
            for cl in sc.get("clips", []):
                total_clips += 1
                cl_id = cl.get("clip_id", "?")
                if not cl.get("person") and _action_has_person(cl.get("action", "")):
                    issues.append(("WARN",
                                   f"{cl_id}: person=false but action mentions "
                                   f"a person: {cl.get('action', '')!r}"))

    if total_clips > cfg.max_clips:
        issues.append(("WARN",
                       f"total clip count {total_clips} exceeds "
                       f"max_clips={cfg.max_clips}"))

    notes = (doc.get("review") or {}).get("notes") or ""
    if notes.strip():
        for line in notes.strip().splitlines()[:8]:
            issues.append(("WARN", f"review.notes: {line}"))

    return issues


# ---------------------------------------------------------------------------
# 3_scenes.json
# ---------------------------------------------------------------------------
def _iter(data: dict) -> Iterator[Tuple[dict, dict]]:
    for scene in data.get("scenes", []):
        for clip in scene.get("clips", []):
            yield scene, clip


def _check_scene_libraries(data: dict) -> List[Issue]:
    out: List[Issue] = []

    ws_list = data.get("wardrobe_states") or []
    ws_ids = [s.get("state_id") for s in ws_list]
    if "default" not in ws_ids:
        out.append(("ERROR", "3_scenes.wardrobe_states missing 'default' sentinel"))
    if len(ws_ids) != len(set(ws_ids)):
        out.append(("ERROR", "3_scenes.wardrobe_states has duplicate state_ids"))

    props_list = data.get("props") or []
    prop_ids = [p.get("prop_id") for p in props_list]
    if "none" not in prop_ids:
        out.append(("ERROR", "3_scenes.props missing 'none' sentinel"))
    if len(prop_ids) != len(set(prop_ids)):
        out.append(("ERROR", "3_scenes.props has duplicate prop_ids"))

    return out


def check_scenes(data: dict) -> List[Issue]:
    out: List[Issue] = []

    if not data.get("characters"):
        out.append(("ERROR", "3_scenes: no characters"))
    if not data.get("scenes"):
        out.append(("ERROR", "3_scenes: no scenes"))
    if not data.get("project"):
        out.append(("ERROR", "3_scenes: missing project block"))

    out.extend(_check_scene_libraries(data))

    chars = data.get("characters") or []
    char_ids = {c.get("character_id") for c in chars}
    wardrobes_by_char = {
        c["character_id"]: {w["wardrobe_id"] for w in c.get("wardrobe", [])}
        for c in chars
    }

    seen: set = set()
    for scene in data.get("scenes", []):
        sid = scene["scene_id"]
        tod = scene.get("time_of_day")
        if tod not in VOCABULARIES["time_of_day"]:
            out.append(("ERROR",
                        f"{sid}: time_of_day {tod!r} not in vocabulary"))
        elif scene.get("light") != LIGHT_PRESETS[tod]:
            out.append(("ERROR",
                        f"{sid}: light does not match '{tod}' preset"))

        env = scene.get("environment") or {}
        if not env.get("name"):
            out.append(("ERROR", f"{sid}: missing environment.name"))
        if not env.get("description"):
            out.append(("ERROR", f"{sid}: missing environment.description"))
        if not env.get("ambience"):
            out.append(("WARN", f"{sid}: missing environment.ambience"))

        for c in scene.get("characters_present", []):
            if char_ids and c not in char_ids:
                out.append(("ERROR", f"{sid}: unknown character {c}"))

        first = True
        for clip in scene.get("clips", []):
            cid = clip["clip_id"]
            if cid in seen:
                out.append(("ERROR", f"duplicate clip_id {cid}"))
            seen.add(cid)
            wid = clip.get("wardrobe_id")
            if not any(wid in s for s in wardrobes_by_char.values()):
                out.append(("ERROR",
                            f"{cid}: unknown wardrobe_id {wid!r}"))
            if first and (clip.get("continuity") or {}).get("transition_in") != "hard_cut":
                out.append(("ERROR",
                            f"{cid}: first clip of scene must be hard_cut"))
            first = False

    if not seen:
        out.append(("ERROR", "3_scenes produced 0 clips"))
    return out


# ---------------------------------------------------------------------------
# 4_shots
# ---------------------------------------------------------------------------
def check_shots(data: dict) -> List[Issue]:
    out: List[Issue] = []
    for _, clip in _iter(data):
        shot = clip.get("shot")
        if not shot:
            continue
        st = shot.get("shot_type")
        if st not in VOCABULARIES["shot_type"]:
            out.append(("ERROR", f"{clip['clip_id']}: bad shot_type {st}"))
        elif shot.get("lens_mm") != SHOT_LENS[st]:
            out.append(("ERROR",
                        f"{clip['clip_id']}: lens {shot.get('lens_mm')} "
                        f"does not match {st}"))
    return out


# ---------------------------------------------------------------------------
# 5_keyframes
# ---------------------------------------------------------------------------
def check_keyframes(data: dict) -> List[Issue]:
    out: List[Issue] = []

    ws_ids = {s["state_id"] for s in (data.get("wardrobe_states") or [])}
    prop_ids = {p["prop_id"] for p in (data.get("props") or [])}

    order: List[str] = []
    scene_of: dict = {}
    for scene, clip in _iter(data):
        order.append(clip["clip_id"])
        scene_of[clip["clip_id"]] = scene["scene_id"]

    for scene, clip in _iter(data):
        cid, kfs = clip["clip_id"], clip.get("keyframes")
        if not kfs:
            continue

        for role in ("first", "middle", "last"):
            kf = kfs.get(role)
            if not kf:
                out.append(("ERROR", f"{cid}: missing {role} keyframe"))
                continue
            if role == "first" and kf.get("reuse_from"):
                prev_id = kf["reuse_from"].rsplit(".", 1)[0]
                if scene_of.get(prev_id) != scene["scene_id"]:
                    out.append(("ERROR",
                                f"{cid}.first: reuse_from crosses scenes or "
                                f"is unknown"))
                elif order.index(prev_id) != order.index(cid) - 1:
                    out.append(("ERROR",
                                f"{cid}.first: reuse_from is not the previous "
                                f"clip"))
                if kf.get("state") or kf.get("image_gen"):
                    out.append(("ERROR",
                                f"{cid}.first: reused keyframe must not carry "
                                f"state/image_gen"))
                continue
            st = kf.get("state") or {}
            if not st:
                out.append(("ERROR", f"{cid}.{role}: missing state"))
                continue

            # pose: must resolve to a concrete pose (no 'pose_new' in saved file)
            pid = st.get("pose_id")
            if pid == "pose_new":
                out.append(("ERROR",
                            f"{cid}.{role}: pose_id still 'pose_new' "
                            f"(unresolved proposal - re-run 'keyframes')"))
            elif pid not in _POSE_IDS:
                out.append(("ERROR",
                            f"{cid}.{role}: pose_id {pid!r} not in "
                            f"pose library"))

            if st.get("wardrobe_state_id") not in ws_ids:
                out.append(("ERROR",
                            f"{cid}.{role}: wardrobe_state_id "
                            f"{st.get('wardrobe_state_id')!r} not in "
                            f"3_scenes.wardrobe_states"))
            if st.get("prop_id") not in prop_ids:
                out.append(("ERROR",
                            f"{cid}.{role}: prop_id "
                            f"{st.get('prop_id')!r} not in 3_scenes.props"))

            for fld in ("screen_horizontal", "screen_depth",
                        "facing", "expression"):
                if st.get(fld) not in VOCABULARIES[fld]:
                    out.append(("ERROR",
                                f"{cid}.{role}: {fld}={st.get(fld)} "
                                f"not in vocabulary"))

            ig = kf.get("image_gen") or {}
            if not ig.get("template_id") or "seed" not in ig:
                out.append(("ERROR", f"{cid}.{role}: image_gen incomplete"))

        first = kfs.get("first") or {}
        last = kfs.get("last") or {}
        if not first.get("reuse_from") and not last.get("reuse_from"):
            fs = (first.get("state") or {}).get("wardrobe_state_id")
            ls = (last.get("state") or {}).get("wardrobe_state_id")
            if fs and ls and fs != ls:
                out.append(("ERROR",
                            f"{cid}: first/last wardrobe_state_id mismatch "
                            f"({fs!r} vs {ls!r})"))

    return out


# ---------------------------------------------------------------------------
# States (library + keyframe-level consistency)
# ---------------------------------------------------------------------------
def check_states(storage) -> List[Issue]:
    """Validate wardrobe_states library shape and per-keyframe consistency."""
    issues: List[Issue] = []

    if storage.scenes_keyframes_exists():
        data = storage.read_scenes_keyframes()
    elif storage.scenes_exists():
        data = storage.read_scenes()
    else:
        return issues

    ws_list = data.get("wardrobe_states") or []
    ws_ids = {s["state_id"] for s in ws_list}
    if not ws_list:
        issues.append(("ERROR", "wardrobe_states library is empty"))
        return issues
    if "default" not in ws_ids:
        issues.append(("ERROR", "wardrobe_states missing 'default' sentinel"))
    for s in ws_list:
        if not s.get("state_id") or not isinstance(s.get("text"), str):
            issues.append(("ERROR",
                           f"wardrobe_states: malformed entry {s!r}"))

    # Keyframe-level consistency only if keyframes exist.
    if not storage.scenes_keyframes_exists():
        return issues

    ws_ids = {s["state_id"] for s in data.get("wardrobe_states") or []}
    total = non_default = 0
    for _, clip in _iter(data):
        kfs = clip.get("keyframes") or {}
        for role in ("first", "middle", "last"):
            kf = kfs.get(role) or {}
            if kf.get("reuse_from") or not kf.get("state"):
                continue
            total += 1
            sid = kf["state"].get("wardrobe_state_id")
            if ws_ids and sid not in ws_ids:
                issues.append(("ERROR",
                               f"{clip['clip_id']}.{role}: "
                               f"wardrobe_state_id {sid!r} not in library"))
            if sid and sid != "default":
                non_default += 1

    if total and non_default == 0:
        issues.append(("WARN",
                       f"5_scenes-keyframes.json: 0/{total} generated frames "
                       f"use a non-default wardrobe_state_id. Either the "
                       f"story has no state change, or the fragmenter did not "
                       f"propose any states and the LLM always picked "
                       f"'default'."))

    return issues


# ---------------------------------------------------------------------------
# 7_qwen-prompts
# ---------------------------------------------------------------------------
def check_prompts(data: dict) -> List[Issue]:
    out: List[Issue] = []
    chars = {c["character_id"]: c for c in data.get("characters", [])}
    for scene, clip in _iter(data):
        env = scene.get("environment") or {}
        for role in ("first", "middle", "last"):
            kf = (clip.get("keyframes") or {}).get(role) or {}
            ig = kf.get("image_gen")
            if not ig:
                continue
            kid, p = kf.get("keyframe_id"), ig.get("rendered_prompt")
            if not p:
                out.append(("ERROR", f"{kid}: no rendered_prompt"))
                continue
            low = _flat(p)
            is_chain_start = (role == "first")
            if _SNAKE.search(p):
                out.append(("ERROR",
                            f"{kid}: raw token in prompt: "
                            f"{_SNAKE.search(p).group(0)}"))
            if "{" in p or "}" in p:
                out.append(("ERROR", f"{kid}: unfilled slot in prompt"))
            if re.search(r"\b(None|null|nan)\b", p):
                out.append(("ERROR", f"{kid}: placeholder in prompt"))
            if re.search(r":\s*\.", p):
                out.append(("ERROR", f"{kid}: empty field in prompt"))
            for phrase in BANNED_PHRASES:
                if re.search(rf"\b{re.escape(phrase)}\b", low):
                    out.append(("ERROR",
                                f"{kid}: banned phrase '{phrase}'"))
            if env.get("description") and _flat(env["description"]) not in low:
                out.append(("ERROR",
                            f"{kid}: environment description missing"))
            if is_chain_start and "lighting:" not in low:
                out.append(("ERROR",
                            f"{kid}: lighting missing on chain-start keyframe"))
            if is_chain_start and "framing:" not in low:
                out.append(("ERROR",
                            f"{kid}: framing missing on chain-start keyframe"))
            if ig.get("template_id") == "qwen_keyframe":
                st = kf.get("state") or {}
                ch = chars.get(st.get("character_id"), {})
                if ch.get("identity_lock") and _flat(ch["identity_lock"]) not in low:
                    out.append(("ERROR", f"{kid}: identity lock missing"))
                if "pose:" not in low:
                    out.append(("ERROR", f"{kid}: pose missing"))
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
# 'breakdown' here refers to the check that reads output/3_scenes.json.
# It's called 'breakdown' for legacy compatibility with main.py; no CLI
# argument uses this key directly.
ALL_STAGES = [
    "templates", "characters", "character", "fragmented",
    "breakdown", "shots", "keyframes", "states", "qwen-prompts",
]


def check_stage(storage, stage: str) -> List[Issue]:
    if stage == "templates":
        return check_templates()
    if stage == "characters":
        entries, issues = load_all_characters(storage)
        if len(entries) > 1:
            issues.append(("ERROR",
                           f"{len(entries)} character files found, but the "
                           f"schema allows only one per file. "
                           f"Multi-character is not yet implemented."))
        return issues
    if stage == "character":
        return load_character_file(storage)[1]
    if stage == "fragmented":
        return check_fragmented_script(storage)
    if stage == "breakdown":
        if not storage.scenes_exists():
            return [("ERROR", "3_scenes.json missing")]
        return check_scenes(storage.read_scenes())
    if stage == "states":
        return check_states(storage)
    if stage == "shots":
        return check_shots(storage.read_scenes_shots()) \
            if storage.scenes_shots_exists() else []
    if stage == "keyframes":
        return check_keyframes(storage.read_scenes_keyframes()) \
            if storage.scenes_keyframes_exists() else []
    if stage == "qwen-prompts":
        return check_prompts(storage.read_qwen_prompts()) \
            if storage.qwen_prompts_exists() else []
    return []


def report(issues: List[Issue], label: str) -> int:
    errors = sum(1 for lvl, _ in issues if lvl == "ERROR")
    print(f"[validate:{label}] {errors} error(s), "
          f"{len(issues) - errors} warning(s)")
    for lvl, msg in issues[:40]:
        print(f"   {lvl}: {msg}")
    if len(issues) > 40:
        print(f"   ... and {len(issues) - 40} more")
    return errors