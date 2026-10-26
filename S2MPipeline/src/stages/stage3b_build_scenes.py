"""Stage 3b - build output/3_scenes.json from fragments + character sheet + project.

Pure code. No LLM. Reads:
  - input/story_fragmented.json          (envs, states, props, sequences)
  - input/char_XX_sheet.json             (character identity, wardrobe)
  - output/1_project.json                (style bible)

Writes output/3_scenes.json with:
  - Denormalized environments (each scene gets its env inline)
  - wardrobe_states library = [default sentinel] + fragment states (deduped)
  - props            library = [none sentinel]    + fragment props (deduped)
  - characters:      from sheet JSONs
  - scenes:          with per-clip wardrobe_id, no per-clip wardrobe_state

Sentinel IDs ('default', 'none') always win on collision, so a fragmenter
that mistakenly re-emits them cannot override the empty-text sentinel that
drops the corresponding sentence in Stage 7's prompt.

Sentinel texts are EMPTY strings so that Stage 7's render_sentences drops
the corresponding sentence for default wardrobe / no-prop keyframes.
"""
from collections import Counter

from reference import LIGHT_PRESETS, VOCABULARIES
from validate import load_all_characters
from .base import PipelineStage
from .stage3a_fragment import _env_slug

_DARK_TIMES = frozenset({"pre_dawn", "dusk", "night"})
_CLAMP_MAP = {
    "pre_dawn": "dawn",
    "dusk":     "golden_hour",
    "night":    "dusk",
}

_WARDROBE_DEFAULT_SENTINEL = {"state_id": "default", "text": ""}
_PROP_NONE_SENTINEL = {"prop_id": "none", "name": "nothing in hands", "text": ""}


def _apply_scene_time(tod: str, cfg) -> str:
    if cfg.force_time_of_day:
        return cfg.force_time_of_day
    if cfg.clamp_dark_lighting and tod in _CLAMP_MAP:
        return _CLAMP_MAP[tod]
    return tod


def _merge_library(sentinel: dict, entries: list, id_key: str) -> list:
    """Prepend sentinel, dedupe by id_key. Sentinel wins on collision."""
    result = [dict(sentinel)]
    seen = {sentinel[id_key]}
    for e in entries:
        eid = e.get(id_key)
        if not eid or eid in seen:
            continue
        seen.add(eid)
        result.append(e)
    return result


class Stage3bBuildScenes(PipelineStage):
    def __init__(self, llm, storage, force: bool = False):
        super().__init__(llm, storage)
        self._force = force

    def run(self) -> None:
        st, cfg = self._storage, self._llm._config

        if not st.fragmented_exists():
            print(f"Error: missing {cfg.fragmented_path}. "
                  f"Run 'breakdown' first.")
            return
        if not st.project_exists():
            print("Error: missing output/1_project.json. "
                  "Run 'project' first.")
            return

        fragments = st.read_fragmented()

        reviewed = bool((fragments.get("review") or {}).get("reviewed"))
        if not reviewed and cfg.require_fragmentation_review:
            print(f"\n{cfg.fragmented_path.name} has review.reviewed=false.")
            print(f"Inspect and edit it, then set review.reviewed to true, "
                  f"then re-run: python main.py scenes")
            notes = (fragments.get("review") or {}).get("notes") or ""
            if notes.strip():
                print(f"\nExisting notes:\n{notes}")
            return
        if not reviewed:
            print(f"Warning: {cfg.fragmented_path.name} is unreviewed; "
                  f"proceeding because require_fragmentation_review=false.")

        if (cfg.force_time_of_day
                and cfg.force_time_of_day not in VOCABULARIES["time_of_day"]):
            print(f"Error: force_time_of_day={cfg.force_time_of_day!r} is not in "
                  f"{VOCABULARIES['time_of_day']}.")
            return

        entries, issues = load_all_characters(st)
        for lvl, msg in issues:
            print(f"  {lvl}: {msg}")
        if any(lvl == "ERROR" for lvl, _ in issues):
            print("\nFix character sheet(s) and re-run: python main.py scenes")
            return
        if not entries:
            print("Error: no character found. Run 'python main.py characters' first.")
            return
        if len(entries) > 1:
            print(f"Error: {len(entries)} characters found; "
                  f"multi-character is not yet supported.")
            return
        entry = entries[0]

        try:
            scenes, stats = self._build(fragments, entry, cfg)
        except ValueError as exc:
            print(f"Error: {exc}")
            return

        wardrobe_states = _merge_library(
            _WARDROBE_DEFAULT_SENTINEL,
            fragments.get("wardrobe_states", []),
            "state_id",
        )
        props = _merge_library(
            _PROP_NONE_SENTINEL,
            fragments.get("props", []),
            "prop_id",
        )

        data = {
            "schema_version": "3.0",
            "project":         st.read_project()["project"],
            "characters":      [self._character_dict(entry, cfg)],
            "wardrobe_states": wardrobe_states,
            "props":           props,
            "scenes":          scenes,
        }
        st.write_scenes(data)

        n_clips = sum(len(s["clips"]) for s in scenes)
        print(f"\nWrote {cfg.scenes_path} "
              f"({len(scenes)} scene(s), {n_clips} clip(s), "
              f"{len(wardrobe_states)} wardrobe state(s), "
              f"{len(props)} prop(s))")
        self._print_light_summary(stats, cfg)

    # ------------------------------------------------------------------
    @staticmethod
    def _character_dict(entry: dict, cfg) -> dict:
        return {
            "character_id":  entry["character_id"],
            "name":          entry["name"],
            "subject":       entry["subject"],
            "identity":      entry["identity"],
            "identity_lock": entry["identity_lock"],
            "wardrobe":      entry["wardrobe"],
            "sheet": {
                "path":   str(cfg.input_path / entry["sheet_file"]),
                "status": "ready",
            },
            "sheet_check": entry["sheet_check"],
            "review":      entry["review"],
        }

    # ------------------------------------------------------------------
    def _build(self, fragments, character, cfg):
        char_id = character["character_id"]
        wardrobes = character.get("wardrobe") or []
        if not wardrobes:
            raise ValueError(f"{char_id}: no wardrobe in sheet JSON")
        wardrobe_id = wardrobes[0]["wardrobe_id"]

        env_by_slug = {
            _env_slug(e["name"]): e
            for e in fragments.get("environments", [])
        }

        raw_counts: Counter = Counter()
        final_counts: Counter = Counter()
        dark = 0
        scenes = []

        for seq in fragments.get("sequences", []):
            seq_id = seq["sequence_id"]
            for sc in seq.get("scenes", []):
                raw_tod = sc["time_of_day"]
                raw_counts[raw_tod] += 1
                if raw_tod in _DARK_TIMES:
                    dark += 1
                scene_tod = _apply_scene_time(raw_tod, cfg)
                final_counts[scene_tod] += 1

                env_slug = _env_slug(sc["environment_name"])
                env = env_by_slug.get(env_slug)
                if not env:
                    valid = ", ".join(sorted(env_by_slug.keys())) or "(none)"
                    raise ValueError(
                        f"{sc['scene_id']}: environment_name "
                        f"{sc['environment_name']!r} (slug {env_slug!r}) "
                        f"not in fragments.environments. "
                        f"Valid slugs: {valid}. "
                        f"Edit story_fragmented.json and re-run scenes."
                    )

                clips = []
                for i, cl in enumerate(sc.get("clips", []), start=1):
                    clips.append({
                        "clip_id":           cl["clip_id"],
                        "clip_number":       cl["clip_number"],
                        "name":              cl["name"],
                        "story_action":      cl["action"],
                        "duration_sec":      cl.get("duration_sec", 5),
                        "continuity": {
                            "transition_in":  "hard_cut" if i == 1
                                              else "continuous_from_previous",
                            "framing_locked": True,
                        },
                        "wardrobe_id":       wardrobe_id,
                        "character_present": bool(cl["person"] and char_id),
                    })

                scenes.append({
                    "scene_id":           sc["scene_id"],
                    "sequence_id":        seq_id,
                    "scene_number":       sc["scene_number"],
                    "name":               sc["name"],
                    "summary":            sc["summary"],
                    "environment": {
                        "name":        env["name"],
                        "description": env["description"],
                        "ambience":    env["ambience"],
                    },
                    "time_of_day":        scene_tod,
                    "weather":            "clear",
                    "light":              dict(LIGHT_PRESETS[scene_tod]),
                    "characters_present": list(sc.get("characters_present", [])),
                    "clips":              clips,
                })

        return scenes, {
            "raw":   dict(raw_counts),
            "final": dict(final_counts),
            "total": sum(len(s["clips"]) for s in scenes),
            "dark":  dark,
        }

    def _print_light_summary(self, stats, cfg) -> None:
        raw = stats.get("raw") or {}
        final = stats.get("final") or {}
        total = stats.get("total") or 0
        dark = stats.get("dark") or 0
        if not total:
            return

        def fmt(d):
            return ", ".join(f"{k}: {v}" for k, v in sorted(
                d.items(), key=lambda kv: (-kv[1], kv[0])))

        print(f"\n  Time of day (fragments): {fmt(raw)}")
        if cfg.force_time_of_day or cfg.clamp_dark_lighting:
            print(f"  Time of day (scene):     {fmt(final)}")
            if cfg.force_time_of_day:
                print(f"  force_time_of_day = {cfg.force_time_of_day!r} "
                      f"(all scenes overridden)")
            else:
                print(f"  clamp_dark_lighting = True "
                      f"(pre_dawn->dawn, dusk->golden_hour, night->dusk)")

        ratio = dark / total
        if ratio > cfg.dark_time_warn_threshold:
            print(f"  ! {dark}/{total} clips use dark times "
                  f"({ratio:.0%}, threshold {cfg.dark_time_warn_threshold:.0%}).")
            print(f"    Set force_time_of_day=\"morning\" or clamp_dark_lighting=True "
                  f"in config.py, then re-run: python main.py scenes")