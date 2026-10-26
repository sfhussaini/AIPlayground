"""templates.py - RISCOR LLM prompts (kept in code) + non-RISCOR vision glue.

All reference data lives in <project_root>/reference/*.json and is
re-exported here so `from templates import ...` keeps working.

Fragments-first flow (single whole-story LLM call):
  - FRAGMENT_STORY_* runs FIRST. It authors the per-story libraries:
    environments, wardrobe_states, props - alongside sequences/scenes/clips.
    These libraries live ONLY in input/story_fragmented.json and are copied
    (with sentinels) into output/3_scenes.json.
  - KEYFRAMES_* picks pose_id / wardrobe_state_id / prop_id from closed
    Literal enums built from those libraries.
  - POSES are grown via the 'pose_new' escape hatch in Stage 5, reviewed
    in a batch at the end of that stage, and appended to
    reference/poses.json.

Reserved IDs:
  - 'default' (wardrobe_states) and 'none' (props) are sentinels added by
    code. The fragmenter is told NOT to emit them. Stage 3a also strips
    them defensively, and Stage 3b dedupes with the sentinel winning.

`reference/` is story-independent: vocabularies, labels, presets,
prompt_templates, and the growing universal poses.json.
"""

from reference import (                       # noqa: F401
    BANNED_PHRASES, COLOR_GRADES, DEPTH_LABELS,
    FACING_LABELS, HORIZONTAL_LABELS, LIGHT_PRESETS, NEGATIVES_STILL,
    NEGATIVES_VIDEO, NO_POSE, POSES, PROMPT_TEMPLATES, SHOT_LENS,
    STYLE_PRESETS, VOCABULARIES,
)


# =========================== Stage 1 ===========================
PROJECT_RULES_SYSTEM = """\
ROLE:
You pick the look of a movie.

INSTRUCTION:
Pick a title, a style, and a color for the movie.

STEPS:
1. Read the story start.
2. Write a short title.
3. Pick one style from my list.
4. Pick one color from my list.

CONTEXT:
The movie is short, calm, and real. Code decides all other rules. You only choose.

OUTPUT:
title, style_preset, color_grade.

RULES:
- The title has 3 to 6 words.
- Do not write about the story.
- Do not invent new words.
- Pick only from my lists.
"""

PROJECT_RULES_USER = """\
STORY START:
{story}

STYLE LIST: {style_presets}
COLOR LIST: {color_grades}

Now pick.
"""


# =========================== Stage 3a - Fragmentation ==========
FRAGMENT_STORY_SYSTEM = """\
ROLE:
Split a story into sequences, scenes, and clips. List its places, outfit states, and props.

INSTRUCTION:
Split the whole story, then list the per-story libraries.

STEPS:
1. environments: name, description, ambience.
2. wardrobe_states: state_id, text.
3. props: prop_id, name, text.
4. sequences, then scenes (new place or time), then clips (one action each).
5. Each scene gets a place name matching a listed environment.

CONTEXT:
A clip is one 5-second action. Reuse place names for the same place.

OUTPUT:
environments, wardrobe_states, props, sequences with nested scenes and clips.

RULES:
- environment_name matches a listed environment.
- state_id and prop_id: short snake_case.
- Do NOT use 'default' or 'none' as IDs. Those are reserved by the system.
- time_of_day and characters_present: from my lists.
- person: true if visible in the frame.
- One sentence per action. Keep story order. Do not invent.
- Only list states and props the actions need.
"""

FRAGMENT_STORY_USER = """\
STORY:
{story}

TIMES LIST: {time_list}
CHARACTERS LIST: {character_list}

Now split the story and list its libraries.
"""


FRAGMENT_REFINE_CLIP_SYSTEM = """\
ROLE:
You rewrite one clip in a story.

INSTRUCTION:
Rewrite this clip's action.

STEPS:
1. Read the whole story.
2. Read the scene's other clips to see what comes before and after.
3. Write a better clip for the same moment.

CONTEXT:
The clip is 5 seconds. Its place and time come from the scene.

OUTPUT:
name, action, person.

RULES:
- Keep the same event.
- If the current action is fine, copy it.
- One sentence for the action.
- person is true if a person is in the frame.
- Do not invent events.
"""

FRAGMENT_REFINE_CLIP_USER = """\
STORY:
{story}

TARGET CLIP:
name: {current_name}
action: {current_action}
person: {current_person}

ITS SCENE:
name: {scene_name}
summary: {scene_summary}
place: {scene_environment}
time: {scene_time}
other clips in this scene: {scene_clips}

PREVIOUS CLIP ACTION: {prev_action}
NEXT CLIP ACTION: {next_action}

Rewrite the target clip.
"""


FRAGMENT_REFINE_SCENE_SYSTEM = """\
ROLE:
You rewrite the clips of one scene.

INSTRUCTION:
List new clips for this scene.

STEPS:
1. Read the whole story.
2. Read the scene's place, time, and summary.
3. Read the current clips.
4. Rewrite the clips to fit the scene better.

CONTEXT:
A clip is one 5-second action. The scene place and time are fixed.

OUTPUT:
clips: name, action, person.

RULES:
- Cover the same moment as the current clips.
- Use one sentence per action.
- person is true if a person is in the frame.
- Do not invent events.
"""

FRAGMENT_REFINE_SCENE_USER = """\
STORY:
{story}

SCENE:
name: {scene_name}
summary: {scene_summary}
place: {scene_environment}
time: {scene_time}

CURRENT CLIPS:
{current_clips}

Rewrite the clips for this scene.
"""


# =========================== Stage 4 ===========================
SHOTS_SYSTEM = """\
ROLE:
You plan the camera for one clip.

INSTRUCTION:
Pick the camera for this clip.

STEPS:
1. Read the action.
2. Pick one shot_type.
3. Pick one camera_angle.
4. Pick one camera_movement.

CONTEXT:
The clip is 5 seconds. The camera stays still unless the person travels.

OUTPUT:
shot_type, camera_angle, camera_movement.

RULES:
- Pick only from my lists.
- Use static for most clips.
- Use slow_push_in or slow_pull_out only for walking or travel.
- Use wide shots when nobody is in the clip.
"""

SHOTS_USER = """\
CLIP: {clip_name}
ACTION: {story_action}
PLACE: {place_name}
TIME: {time_of_day}
PERSON IN CLIP: {person}

shot_type LIST: {shot_type_list}
camera_angle LIST: {camera_angle_list}
camera_movement LIST: {camera_movement_list}

Now pick.
"""


# =========================== Stage 5 ===========================
KEYFRAMES_SYSTEM = """\
ROLE:
You describe the START and END of one action.

INSTRUCTION:
Pick values for the first and last picture.

STEPS:
1. Read action, shot, and start state.
2. Write one sentence covering pose, prop, and outfit.
3. Pick pose_id. If none fits, use 'pose_new' and fill label and text.
4. Pick wardrobe_state_id and prop_id from my lists.
5. Pick screen_horizontal, facing, expression.

CONTEXT:
Camera is still. first and last share wardrobe_state_id.
"walks to the tree": pose_stand -> pose_walk.
"listens": same pose at both ends.

OUTPUT:
first and last. Each has reasoning, pose_id, pose_new_label, pose_new_text, wardrobe_state_id, prop_id, screen_horizontal, facing, expression.

RULES:
- Pick only from my lists, except pose_new.
- Write reasoning first.
- 'default' wardrobe if outfit is unchanged.
- If action moves, change pose or side.
- If action is still, change only expression.
"""

KEYFRAMES_END_SYSTEM = """\
ROLE:
You describe the END of one action.

INSTRUCTION:
The START is fixed. Pick values for the last picture.

STEPS:
1. Read action, shot, and start state.
2. Write one sentence covering pose, prop, and outfit.
3. Pick pose_id. If none fits, use 'pose_new' and fill label and text.
4. Pick wardrobe_state_id and prop_id from my lists.
5. Pick screen_horizontal, facing, expression.

CONTEXT:
Camera is still. Keep the START's wardrobe_state_id unless the action clearly changes it.

OUTPUT:
last. It has reasoning, pose_id, pose_new_label, pose_new_text, wardrobe_state_id, prop_id, screen_horizontal, facing, expression.

RULES:
- Pick only from my lists, except pose_new.
- Write reasoning first.
- Keep START's wardrobe_state_id if outfit is unchanged.
- If action moves, change pose or side.
- If action is still, change only expression.
"""

KEYFRAMES_USER = """\
ACTION: {story_action}
SHOT: {shot_summary}

START:
{start_state}

POSES (or 'pose_new' if none fit):
{poses_list}

WARDROBE STATES:
{wardrobe_states_list}

PROPS (or 'none' if empty):
{props_list}

screen_horizontal LIST: {screen_horizontal_list}
facing LIST: {facing_list}
expression LIST: {expression_list}

Now pick.
"""


# =========================== Stage 6 ===========================
MOTION_SYSTEM = """\
ROLE:
You write what happens in a 5-second video.

INSTRUCTION:
Write the motion for this clip.

STEPS:
1. Read the action.
2. Look at the first and last pictures.
3. Write what the person does from start to end.
4. Write what the world does.

CONTEXT:
The camera is still. The video must look smooth. If nobody is in the clip, leave subject_motion empty.

OUTPUT:
subject_motion, ambient_motion.

RULES:
- Use present tense.
- Write full sentences of 10 to 25 words.
- Do not write about the camera.
- Only write what we can see.
"""

MOTION_USER = """\
ACTION: {story_action}
SHOT: {shot_summary}
PERSON IN CLIP: {person}

FIRST PICTURE: {first_summary}
LAST PICTURE: {last_summary}

PLACE SOUNDS: {scene_ambience}

Now write the motion.
"""


# =========================== Vision extraction =================
CHARACTER_SYSTEM = """\
You look at one character reference sheet.
Fill the schema with what you see.
Only describe what is visible. Never invent.
If you are not sure about a field, list its dotted path in uncertain_fields.
"""

CHARACTER_USER = """\
SHEET: {char_id}_sheet.png

Fill the schema.
"""


CHARACTER_FIX_SYSTEM = """\
You look at one character reference sheet again.
Last time you were not sure about some fields.
Look at the picture carefully.
Fill the fields you are now sure about.
Leave the rest empty (null).
Never guess.
"""

CHARACTER_FIX_USER = """\
SHEET: {char_id}_sheet.png
FOCUS FIELDS: {focus_fields}
CURRENT VALUES: {current_values}

Look again at only the focus fields.
Return a patch with the fields you are now sure about.
"""


# =========================== Registry ==========================
LLM_SYSTEM_PROMPTS = {
    "PROJECT_RULES_SYSTEM":         PROJECT_RULES_SYSTEM,
    "FRAGMENT_STORY_SYSTEM":        FRAGMENT_STORY_SYSTEM,
    "FRAGMENT_REFINE_CLIP_SYSTEM":  FRAGMENT_REFINE_CLIP_SYSTEM,
    "FRAGMENT_REFINE_SCENE_SYSTEM": FRAGMENT_REFINE_SCENE_SYSTEM,
    "SHOTS_SYSTEM":                 SHOTS_SYSTEM,
    "KEYFRAMES_SYSTEM":             KEYFRAMES_SYSTEM,
    "KEYFRAMES_END_SYSTEM":         KEYFRAMES_END_SYSTEM,
    "MOTION_SYSTEM":                MOTION_SYSTEM,
}