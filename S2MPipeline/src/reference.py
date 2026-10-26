"""Loads all reference data from <project_root>/reference/*.json.

Consumers keep importing the same names from `templates`; this module is the
single place that touches the JSON files. Nothing here goes to the LLM
directly - it is either formatted into Literal enums (models.py) or stitched
into a Qwen/LTX prompt by `base.render_sentences`.

The reference folder is a SIBLING of input/ and output/ so that wiping
input/ or output/ never deletes it.

`poses.json` is a GROWING data set: `append_pose()` lets Stage 2 grow it
after user review; `refresh_poses()` mutates POSES in place.

Wardrobe states and condition text are NOT here. They are per-story and
live in output/2_assets.json. `reference/` stays story-independent.
"""
import json
import os
import re
from pathlib import Path
from typing import Any, Optional


_REF_DIR = Path(__file__).resolve().parent / "reference"


def _load(name: str) -> Any:
    path = _REF_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"Missing reference file: {path}\n"
            f"  Expected layout:\n"
            f"    {_REF_DIR}/vocabularies.json\n"
            f"    {_REF_DIR}/shot_lens.json\n"
            f"    {_REF_DIR}/color_grades.json\n"
            f"    {_REF_DIR}/light_presets.json\n"
            f"    {_REF_DIR}/poses.json\n"
            f"    {_REF_DIR}/labels.json\n"
            f"    {_REF_DIR}/negatives.json\n"
            f"    {_REF_DIR}/style_presets.json\n"
            f"    {_REF_DIR}/banned_phrases.json\n"
            f"    {_REF_DIR}/prompt_templates.json"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --- Closed vocabularies ------------------------------------------------
VOCABULARIES: dict[str, list] = _load("vocabularies.json")

# --- Shot type -> lens mm ----------------------------------------------
SHOT_LENS: dict[str, int] = _load("shot_lens.json")

# --- Color grades ------------------------------------------------------
COLOR_GRADES: list[str] = _load("color_grades.json")

# --- Time-of-day light presets ----------------------------------------
LIGHT_PRESETS: dict[str, dict] = _load("light_presets.json")

# --- Pose library ------------------------------------------------------
_poses_doc = _load("poses.json")
POSES: list[dict] = _poses_doc["poses"]
NO_POSE: dict = _poses_doc["no_pose"]

# --- Human-readable labels --------------------------------------------
_labels = _load("labels.json")
HORIZONTAL_LABELS: dict[str, str] = _labels["horizontal"]
DEPTH_LABELS:      dict[str, str] = _labels["depth"]
FACING_LABELS:     dict[str, str] = _labels["facing"]

# --- Negative prompt terms --------------------------------------------
_neg = _load("negatives.json")
NEGATIVES_STILL: list[str] = _neg["still"]
NEGATIVES_VIDEO: list[str] = _neg["video"]

# --- Style presets -----------------------------------------------------
STYLE_PRESETS: dict[str, dict] = _load("style_presets.json")

# --- Banned phrases in Qwen prompts -----------------------------------
BANNED_PHRASES: list[str] = _load("banned_phrases.json")

# --- Sentence templates for Qwen / LTX --------------------------------
PROMPT_TEMPLATES: dict[str, list[str]] = _load("prompt_templates.json")


# =========================================================================
# Pose library mutation (Stage 2)
# =========================================================================
def refresh_poses() -> list[dict]:
    """Re-read poses.json and mutate POSES in place."""
    doc = _load("poses.json")
    POSES[:] = doc["poses"]
    return POSES


def validate_new_pose(pose: dict, existing: list[dict]) -> Optional[str]:
    pid = pose.get("pose_id", "")
    if not re.match(r"^pose_[a-z][a-z0-9_]*$", pid):
        return f"pose_id must match ^pose_[a-z][a-z0-9_]*$, got {pid!r}"
    if pid == "pose_none":
        return "pose_id 'pose_none' is reserved"
    if any(p["pose_id"] == pid for p in existing):
        return f"pose_id {pid!r} already exists"

    label = (pose.get("label") or "").strip()
    if not label or len(label) > 40:
        return "label must be 1-40 characters"

    text = (pose.get("text") or "").strip()
    if len(text) < 10 or len(text) > 300:
        return "text must be 10-300 characters"

    allowed = {"pose_id", "label", "text", "source"}
    extra = set(pose) - allowed
    if extra:
        return f"extra keys: {sorted(extra)}"
    return None


def append_pose(pose: dict, persist: bool = True) -> None:
    if persist:
        doc = _load("poses.json")
        err = validate_new_pose(pose, doc["poses"])
        if err:
            raise ValueError(err)
        doc["poses"].append(pose)
        _atomic_write_json(_REF_DIR / "poses.json", doc)
        POSES[:] = doc["poses"]
    else:
        err = validate_new_pose(pose, POSES)
        if err:
            raise ValueError(err)
        POSES.append(pose)


__all__ = [
    "VOCABULARIES", "SHOT_LENS", "COLOR_GRADES", "LIGHT_PRESETS",
    "POSES", "NO_POSE",
    "HORIZONTAL_LABELS", "DEPTH_LABELS", "FACING_LABELS",
    "NEGATIVES_STILL", "NEGATIVES_VIDEO",
    "STYLE_PRESETS", "BANNED_PHRASES", "PROMPT_TEMPLATES",
    "refresh_poses", "validate_new_pose", "append_pose",
]