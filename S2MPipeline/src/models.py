"""All Pydantic schemas. Closed vocabularies use Literal types.

Fragments-first, story-local libraries:
  - The fragmenter (Stage 3a) authors per-story libraries in
    input/story_fragmented.json: environments, wardrobe_states, props.
  - Stage 3b copies them into output/3_scenes.json (with sentinels
    'default' and 'none' prepended).
  - Stage 5 picks IDs from closed Literal enums per keyframe.
  - reference/ stays story-independent: vocabularies, labels, presets,
    prompt_templates, and the growing universal poses.json.

Pose escape hatch:
  - `pose_id` is a Literal[existing..., "pose_new"].
  - If pose_id=="pose_new", `pose_new_label` and `pose_new_text` are
    required. Proposals are batched and reviewed at the end of Stage 5;
    accepted poses are appended to reference/poses.json.

No `2_assets.json`:
  - character data comes from input/char_XX_sheet.json
  - pose library from reference/poses.json
  - per-story libraries from 3_scenes.json
"""
from typing import Annotated, List, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

from templates import COLOR_GRADES, STYLE_PRESETS, VOCABULARIES


def _lit(values: Sequence):
    return Literal[tuple(values)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# =========================== Stage 1 ===========================
class ProjectLLM(BaseModel):
    title: str = Field(description="Short movie title, 3-6 words.")
    style_preset: _lit(list(STYLE_PRESETS))
    color_grade: _lit(COLOR_GRADES)


# =========================== Per-story libraries ===============
class FragmentEnvEntry(_Strict):
    name: str = Field(min_length=2, max_length=40,
                      description="Short place name, e.g. 'kitchen'.")
    description: str = Field(min_length=10, max_length=200,
                             description="One sentence describing the place.")
    ambience: str = Field(min_length=5, max_length=120,
                          description="Short ambient-sound note.")

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v):
        import re
        if not re.match(r"^[a-z][a-z0-9_ ]*$", v.lower()):
            raise ValueError("name must be lowercase letters, digits, "
                             "underscore, or space")
        return v


class WardrobeStateEntry(_Strict):
    state_id: str = Field(pattern=r"^[a-z][a-z_]*$",
                          min_length=2, max_length=30,
                          description="snake_case state key, e.g. 'wet'.")
    text: str = Field(min_length=3, max_length=80,
                      description="Short phrase, e.g. 'wet clothes'.")


class PropEntry(_Strict):
    prop_id: str = Field(pattern=r"^prop_[a-z][a-z0-9_]*$",
                         min_length=5, max_length=40,
                         description="snake_case prop key with prop_ prefix, "
                                     "e.g. 'prop_umbrella'.")
    name: str = Field(min_length=2, max_length=30,
                      description="Human-readable name, e.g. 'umbrella'.")
    text: str = Field(min_length=3, max_length=100,
                      description="Full phrase, e.g. 'a closed red umbrella'.")


# =========================== Fragmented story (saved) ==========
class FragmentedSource(_Strict):
    file: str = Field(description="Relative path to the source story file.")
    word_count: int = Field(ge=1)
    sha1: str = Field(pattern=r"^[0-9a-f]{40}$")


class FragmentedReview(_Strict):
    reviewed: bool = Field(
        description="Must be set to true by a HUMAN after checking.")
    notes: str = Field(default="", max_length=2000)


class FragmentedClip(_Strict):
    clip_id: str = Field(pattern=r"^sq_\d{2}\.sc_\d{2}\.c\d{2}$")
    clip_number: int = Field(ge=1)
    name: str = Field(min_length=2, max_length=40)
    action: str = Field(min_length=10, max_length=200)
    person: bool
    duration_sec: int = Field(default=5, ge=1, le=30)


class FragmentedScene(_Strict):
    scene_id: str = Field(pattern=r"^sq_\d{2}\.sc_\d{2}$")
    scene_number: int = Field(ge=1)
    name: str = Field(min_length=2, max_length=40)
    summary: str = Field(min_length=10, max_length=200)
    environment_name: str = Field(min_length=2, max_length=40)
    time_of_day: _lit(VOCABULARIES["time_of_day"])
    characters_present: List[str] = Field(min_length=1, max_length=4)
    clips: List[FragmentedClip] = Field(min_length=1, max_length=12)


class FragmentedSequence(_Strict):
    sequence_id: str = Field(pattern=r"^sq_\d{2}$")
    sequence_number: int = Field(ge=1)
    name: str = Field(min_length=2, max_length=40)
    summary: str = Field(min_length=10, max_length=200)
    scenes: List[FragmentedScene] = Field(min_length=1, max_length=8)


class StoryFragmented(_Strict):
    schema_version: Literal["1.0"]
    source: FragmentedSource
    environments: List[FragmentEnvEntry] = Field(
        default_factory=list, max_length=5)
    wardrobe_states: List[WardrobeStateEntry] = Field(
        default_factory=list, max_length=5)
    props: List[PropEntry] = Field(
        default_factory=list, max_length=10)
    sequences: List[FragmentedSequence] = Field(min_length=1, max_length=3)
    review: FragmentedReview


# =========================== Fragmented story (LLM) ============
class FragmentedClipLLM(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    action: str = Field(min_length=10, max_length=200)
    person: bool


class FragmentedSceneLLM(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    summary: str = Field(min_length=10, max_length=200)
    environment_name: str = Field(min_length=2, max_length=40)
    time_of_day: _lit(VOCABULARIES["time_of_day"])
    characters_present: List[str] = Field(min_length=1, max_length=4)
    clips: List[FragmentedClipLLM] = Field(min_length=1, max_length=12)


class FragmentedSequenceLLM(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    summary: str = Field(min_length=10, max_length=200)
    scenes: List[FragmentedSceneLLM] = Field(min_length=1, max_length=8)


class FragmentedStoryLLM(BaseModel):
    environments: List[FragmentEnvEntry] = Field(
        default_factory=list, max_length=5)
    wardrobe_states: List[WardrobeStateEntry] = Field(
        default_factory=list, max_length=5)
    props: List[PropEntry] = Field(
        default_factory=list, max_length=10)
    sequences: List[FragmentedSequenceLLM] = Field(min_length=1, max_length=3)


class RefineClipLLM(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    action: str = Field(min_length=10, max_length=200)
    person: bool


class RefineSceneLLM(BaseModel):
    clips: List[FragmentedClipLLM] = Field(min_length=1, max_length=12)


# =========================== character schema ==================
class CharHair(_Strict):
    length: Literal["bald", "short", "chin_length", "shoulder_length",
                    "mid_back", "waist_length"]
    style: Literal["straight", "wavy", "curly", "braided", "bun",
                   "ponytail", "tied_back", "other"]
    color: Literal["black", "dark_brown", "brown", "light_brown",
                   "blonde", "red", "grey", "white"]


class CharIdentity(_Strict):
    age_range: Literal["child", "teen", "young_adult", "adult",
                       "middle_aged", "senior"]
    build: Literal["slim", "average", "athletic", "curvy", "stocky", "heavy"]
    face_shape: Literal["oval", "round", "square", "heart", "long"]
    skin_tone: Literal["fair", "light", "medium", "olive", "tan",
                       "brown", "dark_brown", "deep"]
    eye_color: Literal["black", "dark_brown", "brown", "hazel",
                       "green", "blue", "grey"]
    hair: CharHair
    facial_hair: Literal["none", "stubble", "mustache", "beard"]
    facial_marks: List[Annotated[str, Field(max_length=40)]] = Field(
        default_factory=list, max_length=3)
    accessories: List[Annotated[str, Field(max_length=40)]] = Field(
        default_factory=list, max_length=4)


class CharGarment(_Strict):
    type: str = Field(max_length=30)
    color: str = Field(max_length=30)
    pattern: Literal["solid", "striped", "floral", "checked", "printed", "other"]


class CharWardrobe(_Strict):
    wardrobe_id: str = Field(pattern=r"^wd_\d{2}$")
    text: str = Field(min_length=10, max_length=200)
    garments: List[CharGarment] = Field(min_length=1, max_length=5)
    footwear: str = Field(max_length=40)
    # Kept for schema compatibility with Stage 1.5's output. Always contains
    # only {"default": <sheet description>}. Per-story wardrobe states are
    # authored by the fragmenter and live in 3_scenes.json, not here.
    states: dict[str, str] = Field(default_factory=dict)

    @field_validator("states")
    @classmethod
    def _states_ok(cls, v):
        if "default" not in v:
            raise ValueError("states must contain 'default'")
        return v


class CharWardrobeLLM(_Strict):
    text: str = Field(min_length=10, max_length=200)
    garments: List[CharGarment] = Field(min_length=1, max_length=5)
    footwear: str = Field(max_length=40)


class CharSheetCheck(_Strict):
    full_body: bool
    plain_background: bool
    views_visible: List[Literal["front", "side", "back", "three_quarter"]]
    issues: List[str] = Field(default_factory=list)


class CharReview(_Strict):
    source: Literal["vision", "manual"]
    reviewed: bool
    uncertain_fields: List[str] = Field(default_factory=list)


class CharacterEntry(_Strict):
    character_id: str = Field(pattern=r"^char_\d{2}$")
    name: str = Field(min_length=1, max_length=30)
    subject: Literal["woman", "man", "girl", "boy", "person"]
    sheet_file: str = Field(pattern=r"^char_\d{2}_sheet\.png$")
    identity: CharIdentity
    identity_lock: str = Field(min_length=40, max_length=300)
    wardrobe: List[CharWardrobe] = Field(min_length=1, max_length=2)
    sheet_check: CharSheetCheck
    review: CharReview


class CharacterFile(_Strict):
    schema_version: Literal["1.0"]
    characters: List[CharacterEntry] = Field(min_length=1, max_length=1)


# =========================== Vision extraction =================
class CharExtractionLLM(BaseModel):
    subject: Literal["woman", "man", "girl", "boy", "person"]
    identity: CharIdentity
    wardrobe: List[CharWardrobeLLM] = Field(min_length=1, max_length=2)
    sheet_check: CharSheetCheck
    uncertain_fields: List[str] = Field(default_factory=list)


class CharFixHair(_Strict):
    length: Optional[Literal["bald", "short", "chin_length",
                              "shoulder_length", "mid_back",
                              "waist_length"]] = None
    style: Optional[Literal["straight", "wavy", "curly", "braided",
                             "bun", "ponytail", "tied_back", "other"]] = None
    color: Optional[Literal["black", "dark_brown", "brown", "light_brown",
                             "blonde", "red", "grey", "white"]] = None


class CharFixIdentity(_Strict):
    age_range: Optional[Literal["child", "teen", "young_adult",
                                 "adult", "middle_aged", "senior"]] = None
    build: Optional[Literal["slim", "average", "athletic", "curvy",
                             "stocky", "heavy"]] = None
    face_shape: Optional[Literal["oval", "round", "square",
                                  "heart", "long"]] = None
    skin_tone: Optional[Literal["fair", "light", "medium", "olive", "tan",
                                 "brown", "dark_brown", "deep"]] = None
    eye_color: Optional[Literal["black", "dark_brown", "brown", "hazel",
                                 "green", "blue", "grey"]] = None
    hair: Optional[CharFixHair] = None
    facial_hair: Optional[Literal["none", "stubble", "mustache",
                                   "beard"]] = None
    facial_marks: Optional[List[Annotated[str, Field(max_length=40)]]] = None
    accessories: Optional[List[Annotated[str, Field(max_length=40)]]] = None


class CharFixSheetCheck(_Strict):
    full_body: Optional[bool] = None
    plain_background: Optional[bool] = None
    views_visible: Optional[List[Literal["front", "side", "back",
                                          "three_quarter"]]] = None
    issues: Optional[List[str]] = None


class CharFixLLM(BaseModel):
    subject: Optional[Literal["woman", "man", "girl", "boy", "person"]] = None
    identity: Optional[CharFixIdentity] = None
    sheet_check: Optional[CharFixSheetCheck] = None


# =========================== Stage 4 ===========================
class Shot(BaseModel):
    shot_type: _lit(VOCABULARIES["shot_type"])
    camera_angle: _lit(VOCABULARIES["camera_angle"])
    camera_movement: _lit(VOCABULARIES["camera_movement"])


# =========================== Stage 5 ===========================
def make_keyframe_models(
    pose_ids: Sequence[str],
    wardrobe_state_ids: Sequence[str],
    prop_ids: Sequence[str],
):
    """Return (Full with first+last, End with last only).

    Inputs:
      - pose_ids: from reference/poses.json
      - wardrobe_state_ids: from 3_scenes.json wardrobe_states library
        (always includes 'default' sentinel)
      - prop_ids: from 3_scenes.json props library (always includes 'none')

    Pose escape hatch: if pose_id=='pose_new', pose_new_label and
    pose_new_text MUST be filled. Enforced in code after the LLM call.

    `reasoning` is placed BEFORE the enum fields, per the small-model
    constraint-tax literature ("reason free, constrain late"). It is not
    persisted into the keyframe state.
    """
    if not pose_ids:
        raise ValueError("pose_ids is required")
    if not wardrobe_state_ids:
        raise ValueError("wardrobe_state_ids is required")
    if not prop_ids:
        raise ValueError("prop_ids is required")

    pose_enum = list(pose_ids) + ["pose_new"]

    State = create_model(
        "KeyframeStateLLM",
        reasoning=(str, Field(
            default="",
            description=(
                "One short sentence: what the character is doing, what "
                "is in their hands, and how the outfit looks. Write this "
                "before the other fields. "
                "Example: 'She reaches for the shelf, a cup in one hand, "
                "clothes still wet from the rain.'"))),
        pose_id=(_lit(pose_enum), Field(
            description=(
                "Pick from the pose list. Use 'pose_new' only when no "
                "listed pose fits the action."))),
        pose_new_label=(Optional[str], Field(
            default=None,
            description=(
                "Only when pose_id=='pose_new'. 2-40 chars, present "
                "participle, e.g. 'reaching for the shelf'."))),
        pose_new_text=(Optional[str], Field(
            default=None,
            description=(
                "Only when pose_id=='pose_new'. 10-300 chars, neutral "
                "description of the body pose."))),
        wardrobe_state_id=(_lit(wardrobe_state_ids), Field(
            description=(
                "Pick from the wardrobe states listed in the prompt. "
                "Use 'default' when the outfit matches the reference "
                "sheet (unchanged by the action)."))),
        prop_id=(_lit(prop_ids), Field(
            description=(
                "Pick from the props listed. Use 'none' when nothing is "
                "in the hands."))),
        screen_horizontal=(_lit(VOCABULARIES["screen_horizontal"]), ...),
        facing=(_lit(VOCABULARIES["facing"]), ...),
        expression=(_lit(VOCABULARIES["expression"]), ...),
    )
    Full = create_model("KeyframesLLM", first=(State, ...), last=(State, ...))
    End = create_model("KeyframesEndLLM", last=(State, ...))
    return Full, End


# =========================== Stage 6 ===========================
class VideoLLM(BaseModel):
    subject_motion: str
    ambient_motion: str