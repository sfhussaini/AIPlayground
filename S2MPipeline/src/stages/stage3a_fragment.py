"""Stage 3a - fragment the story into Sequences -> Scenes -> Clips,
plus per-story libraries (environments, wardrobe_states, props).

Single whole-story LLM call. Runs FIRST, before project/scenes.
The fragmenter authors:
  - environments:      name, description, ambience
  - wardrobe_states:   state_id, text (e.g. "wet": "wet clothes")
  - props:             prop_id, name, text (e.g. "prop_umbrella": "a red umbrella")
  - sequences / scenes / clips

Reserved IDs:
  - 'default' and 'none' are sentinels added by Stage 3b. The fragmenter is
    told NOT to emit them; this stage also strips them if it happens anyway
    (defence in depth).
"""
import re
import time
from datetime import datetime
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import ValidationError

from models import (
    FragmentEnvEntry, FragmentedClip, FragmentedReview, FragmentedScene,
    FragmentedSequence, FragmentedSource, FragmentedStoryLLM, PropEntry,
    RefineClipLLM, RefineSceneLLM, StoryFragmented, WardrobeStateEntry,
)
from templates import (
    FRAGMENT_REFINE_CLIP_SYSTEM, FRAGMENT_REFINE_CLIP_USER,
    FRAGMENT_REFINE_SCENE_SYSTEM, FRAGMENT_REFINE_SCENE_USER,
    FRAGMENT_STORY_SYSTEM, FRAGMENT_STORY_USER,
    VOCABULARIES,
)
from .base import PipelineStage


_PERSON_WORDS = frozenset({
    "she", "he", "her", "his", "him", "they", "them",
    "woman", "man", "girl", "boy", "person", "character",
    "mother", "father", "sister", "brother", "child",
})

_RESERVED_STATE_IDS = frozenset({"default"})
_RESERVED_PROP_IDS = frozenset({"none"})


def _action_has_person(action: str) -> bool:
    words = re.findall(r"\w+", (action or "").lower())
    return any(w in _PERSON_WORDS for w in words)


def _env_slug(name: str) -> str:
    s = re.sub(r"^(the|a|an|her|his|their|its)\s+",
               "", (name or "").lower().strip())
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "place"


class Stage3aFragment(PipelineStage):
    def __init__(self, llm, storage,
                 re_fragment: bool = False,
                 refine_id: Optional[str] = None):
        super().__init__(llm, storage)
        self._re_fragment = re_fragment
        self._refine_id = refine_id

    # ------------------------------------------------------------------
    def run(self) -> None:
        st = self._storage

        if self._re_fragment and self._refine_id:
            print("Error: --re-fragment and --refine are mutually exclusive.")
            return

        if self._re_fragment:
            st.delete_fragmented()
            self._full_run()
            return

        if self._refine_id:
            self._refine(self._refine_id)
            return

        if not st.fragmented_exists():
            self._full_run()
            return

        try:
            doc = st.read_fragmented()
        except Exception as exc:
            print(f"Error: {st._config.fragmented_path} is not valid JSON: {exc}")
            print(f"  Run: python main.py breakdown --re-fragment")
            return

        saved_sha1 = (doc.get("source") or {}).get("sha1")
        current_sha1 = st.story_sha1()
        if saved_sha1 and saved_sha1 != current_sha1:
            print(f"\nError: {st._config.story_path.name} has changed since "
                  f"{st._config.fragmented_path.name} was written.")
            print(f"  existing sha1: {saved_sha1}")
            print(f"  current sha1:  {current_sha1}")
            print(f"\n  Options:")
            print(f"    1. python main.py breakdown --re-fragment   (regenerate)")
            print(f"    2. restore {st._config.story_path.name}")
            return

        reviewed = bool((doc.get("review") or {}).get("reviewed"))
        print(f"Using existing fragments ({st._config.fragmented_path.name}, "
              f"reviewed={'yes' if reviewed else 'no'}). "
              f"Use --re-fragment to regenerate.")

    # ------------------------------------------------------------------
    def _full_run(self) -> None:
        st, cfg = self._storage, self._llm._config
        if not self._check_inputs(need_story=True):
            return

        story = st.read_story()
        word_count = len(story.split())
        if word_count < 10:
            print(f"Error: story is too short ({word_count} words).")
            return

        char_ids = self._character_ids()
        if not char_ids:
            print("Warning: no character sheet found. "
                  "Run 'python main.py characters' first if the story has people.")

        time_vocab = list(VOCABULARIES["time_of_day"])

        print(f"\nFragmenting {word_count}-word story in one LLM call...")
        t0 = time.perf_counter()

        llm = self._llm.structured(
            FragmentedStoryLLM, temperature=cfg.fragmentation_temperature)
        chain = ChatPromptTemplate.from_messages([
            ("system", FRAGMENT_STORY_SYSTEM),
            ("human",  FRAGMENT_STORY_USER),
        ]) | llm

        try:
            result: FragmentedStoryLLM = self._invoke_with_retry(chain, {
                "story":          story,
                "time_list":      ", ".join(time_vocab),
                "character_list": ", ".join(char_ids) or "(none)",
            })
        except Exception as exc:
            print(f"Error: fragmentation failed: {type(exc).__name__}: {exc}")
            return

        if not result.sequences:
            print("Error: model returned no sequences.")
            return

        sequences = self._assign_ids(result)

        # Strip reserved sentinel IDs (defensive - prompt tells the LLM not to).
        envs = list(result.environments)
        raw_states = list(result.wardrobe_states)
        raw_props = list(result.props)
        states = [s for s in raw_states if s.state_id not in _RESERVED_STATE_IDS]
        props = [p for p in raw_props if p.prop_id not in _RESERVED_PROP_IDS]

        if len(states) < len(raw_states):
            print("  warning: stripped reserved state_id 'default' "
                  "from fragmenter output")
        if len(props) < len(raw_props):
            print("  warning: stripped reserved prop_id 'none' "
                  "from fragmenter output")

        errors = self._validate(
            {"environments": envs, "wardrobe_states": states, "props": props},
            sequences, cfg.max_clips, char_ids,
        )

        doc = StoryFragmented(
            schema_version="1.0",
            source=FragmentedSource(
                file=str(st._config.story_path),
                word_count=word_count,
                sha1=st.story_sha1(),
            ),
            environments=envs,
            wardrobe_states=states,
            props=props,
            sequences=sequences,
            review=FragmentedReview(
                reviewed=False,
                notes="\n".join(errors) if errors else "",
            ),
        )
        st.write_fragmented(doc.model_dump())

        elapsed = time.perf_counter() - t0
        n_scenes = sum(len(s.scenes) for s in sequences)
        n_clips = sum(len(sc.clips) for s in sequences for sc in s.scenes)
        print(f"\n\u2713 Wrote {st._config.fragmented_path} in {elapsed:.1f}s")
        print(f"  {len(sequences)} sequence(s), {n_scenes} scene(s), "
              f"{n_clips} clip(s)")
        print(f"  {len(envs)} environment(s), {len(states)} wardrobe state(s), "
              f"{len(props)} prop(s)")
        if errors:
            print(f"  ! {len(errors)} validation issue(s) - see review.notes")
            for e in errors[:5]:
                print(f"    {e}")
            if len(errors) > 5:
                print(f"    ... and {len(errors) - 5} more")
        print(f"\n  Inspect {st._config.fragmented_path.name}, edit if needed,")
        print(f"  then set review.reviewed to true and re-run: "
              f"python main.py breakdown")

    # ------------------------------------------------------------------
    def _character_ids(self) -> list[str]:
        st = self._storage
        ids: list[str] = []
        for _char_id, raw in st.iter_character_sheet_jsons():
            if raw is None:
                continue
            try:
                for c in raw.get("characters", []):
                    if c.get("character_id"):
                        ids.append(c["character_id"])
            except Exception:
                continue
        if ids:
            return sorted(set(ids))
        if st.character_exists():
            try:
                raw = st.read_character_raw()
                for c in raw.get("characters", []):
                    if c.get("character_id"):
                        ids.append(c["character_id"])
            except Exception:
                pass
        return sorted(set(ids))

    # ------------------------------------------------------------------
    def _refine(self, target_id: str) -> None:
        st, cfg = self._storage, self._llm._config

        if not st.fragmented_exists():
            print("Error: no fragments to refine. Run: python main.py breakdown")
            return

        try:
            doc = StoryFragmented.model_validate(st.read_fragmented())
        except ValidationError as exc:
            print(f"Error: existing fragments do not validate: {exc}")
            print(f"  Run: python main.py breakdown --re-fragment")
            return

        current_sha1 = st.story_sha1()
        if doc.source.sha1 != current_sha1:
            print(f"Error: {st._config.story_path.name} has changed since "
                  f"fragments were written.")
            print(f"  Use --re-fragment to regenerate.")
            return

        parts = target_id.split(".")
        if len(parts) == 1:
            print(f"Error: sequence-level refine is not supported "
                  f"(use --re-fragment to redo everything).")
            return
        level, sc, cl = self._locate(doc, target_id)
        if level is None:
            print(f"Error: no fragment with id {target_id!r}")
            self._print_known_ids(doc)
            return

        story = st.read_story()
        t0 = time.perf_counter()

        print(f"Refining {target_id} ({level})...")
        try:
            if level == "clip":
                self._refine_clip(story, sc, cl)
            else:
                self._refine_scene(story, sc)
        except Exception as exc:
            print(f"Error: refine failed: {type(exc).__name__}: {exc}")
            return

        timestamp = datetime.now().isoformat(timespec="seconds")
        note_line = f"refined {target_id} at {timestamp}"
        doc.review.reviewed = False
        doc.review.notes = (
            (doc.review.notes + "\n") if doc.review.notes else ""
        ) + note_line

        st.write_fragmented(doc.model_dump())

        elapsed = time.perf_counter() - t0
        print(f"\n\u2713 Refined {target_id} in {elapsed:.1f}s")
        print(f"  review.reviewed reset to false")
        print(f"\n  Inspect, set review.reviewed to true, then re-run: "
              f"python main.py breakdown")

    @staticmethod
    def _locate(doc, target_id: str):
        parts = target_id.split(".")
        if len(parts) == 2:
            for seq in doc.sequences:
                for sc in seq.scenes:
                    if sc.scene_id == target_id:
                        return ("scene", sc, None)
        elif len(parts) == 3:
            for seq in doc.sequences:
                for sc in seq.scenes:
                    for cl in sc.clips:
                        if cl.clip_id == target_id:
                            return ("clip", sc, cl)
        return (None, None, None)

    @staticmethod
    def _print_known_ids(doc) -> None:
        ids = []
        for s in doc.sequences:
            ids.append(s.sequence_id)
            for sc in s.scenes:
                ids.append(sc.scene_id)
                for cl in sc.clips:
                    ids.append(cl.clip_id)
        shown = ", ".join(ids[:20])
        tail = " ..." if len(ids) > 20 else ""
        print(f"  Known ids: {shown}{tail}")

    def _refine_clip(self, story: str, sc, cl) -> None:
        cfg = self._llm._config

        prev_action = next_action = "(none)"
        for i, c in enumerate(sc.clips):
            if c.clip_id == cl.clip_id:
                if i > 0:
                    prev_action = sc.clips[i - 1].action
                if i < len(sc.clips) - 1:
                    next_action = sc.clips[i + 1].action
                break

        scene_clips_text = "\n".join(
            f"- {c.name}: {c.action}" for c in sc.clips
            if c.clip_id != cl.clip_id
        ) or "(none)"

        chain = ChatPromptTemplate.from_messages([
            ("system", FRAGMENT_REFINE_CLIP_SYSTEM),
            ("human",  FRAGMENT_REFINE_CLIP_USER),
        ]) | self._llm.structured(
            RefineClipLLM, temperature=cfg.fragmentation_temperature)

        result = self._invoke_with_retry(chain, {
            "story":             story,
            "current_name":      cl.name,
            "current_action":    cl.action,
            "current_person":    "true" if cl.person else "false",
            "scene_name":        sc.name,
            "scene_summary":     sc.summary,
            "scene_environment": sc.environment_name,
            "scene_time":        sc.time_of_day,
            "scene_clips":       scene_clips_text,
            "prev_action":       prev_action,
            "next_action":       next_action,
        })

        cl.name   = result.name.strip()
        cl.action = result.action.strip()
        cl.person = bool(result.person)

    def _refine_scene(self, story: str, sc) -> None:
        cfg = self._llm._config

        current_lines = [
            f"- {cl.name} | {cl.action} | "
            f"person={'yes' if cl.person else 'no'}"
            for cl in sc.clips
        ]

        chain = ChatPromptTemplate.from_messages([
            ("system", FRAGMENT_REFINE_SCENE_SYSTEM),
            ("human",  FRAGMENT_REFINE_SCENE_USER),
        ]) | self._llm.structured(
            RefineSceneLLM, temperature=cfg.fragmentation_temperature)

        result = self._invoke_with_retry(chain, {
            "story":             story,
            "scene_name":        sc.name,
            "scene_summary":     sc.summary,
            "scene_environment": sc.environment_name,
            "scene_time":        sc.time_of_day,
            "current_clips":     "\n".join(current_lines),
        })

        sc.clips = [
            FragmentedClip(
                clip_id=f"{sc.scene_id}.c{k:02d}",
                clip_number=k,
                name=cl_llm.name.strip(),
                action=cl_llm.action.strip(),
                person=bool(cl_llm.person),
                duration_sec=5,
            )
            for k, cl_llm in enumerate(result.clips, start=1)
        ]

    # ------------------------------------------------------------------
    # Assembly + validation
    # ------------------------------------------------------------------
    @staticmethod
    def _assign_ids(result: FragmentedStoryLLM) -> list:
        sequences = []
        for i, seq_llm in enumerate(result.sequences, start=1):
            seq_id = f"sq_{i:02d}"
            scenes = []
            for j, sc_llm in enumerate(seq_llm.scenes, start=1):
                sc_id = f"{seq_id}.sc_{j:02d}"
                clips = [
                    FragmentedClip(
                        clip_id=f"{sc_id}.c{k:02d}",
                        clip_number=k,
                        name=cl_llm.name.strip(),
                        action=cl_llm.action.strip(),
                        person=bool(cl_llm.person),
                        duration_sec=5,
                    )
                    for k, cl_llm in enumerate(sc_llm.clips, start=1)
                ]
                scenes.append(FragmentedScene(
                    scene_id=sc_id,
                    scene_number=j,
                    name=sc_llm.name.strip(),
                    summary=sc_llm.summary.strip(),
                    environment_name=sc_llm.environment_name.strip(),
                    time_of_day=sc_llm.time_of_day,
                    characters_present=list(sc_llm.characters_present),
                    clips=clips,
                ))
            sequences.append(FragmentedSequence(
                sequence_id=seq_id,
                sequence_number=i,
                name=seq_llm.name.strip(),
                summary=seq_llm.summary.strip(),
                scenes=scenes,
            ))
        return sequences

    @staticmethod
    def _validate(libraries: dict, sequences, max_clips: int,
                  char_ids: list[str]) -> list:
        errors = []
        if not sequences:
            return ["no sequences produced"]

        envs: list[FragmentEnvEntry] = libraries["environments"]
        states: list[WardrobeStateEntry] = libraries["wardrobe_states"]
        props: list[PropEntry] = libraries["props"]

        env_names_lower = [e.name.lower() for e in envs]
        if len(env_names_lower) != len(set(env_names_lower)):
            errors.append("duplicate environment names in 'environments' list")

        state_ids = [s.state_id for s in states]
        if len(state_ids) != len(set(state_ids)):
            errors.append("duplicate state_ids in 'wardrobe_states' list")
        for sid in state_ids:
            if sid in _RESERVED_STATE_IDS:
                errors.append(
                    f"wardrobe_states: state_id {sid!r} is reserved; "
                    f"remove it (the sentinel is added by code)")

        prop_ids = [p.prop_id for p in props]
        if len(prop_ids) != len(set(prop_ids)):
            errors.append("duplicate prop_ids in 'props' list")
        for pid in prop_ids:
            if pid in _RESERVED_PROP_IDS:
                errors.append(
                    f"props: prop_id {pid!r} is reserved; "
                    f"remove it (the sentinel is added by code)")

        env_slugs = {_env_slug(n) for n in env_names_lower}
        for seq in sequences:
            for sc in seq.scenes:
                if _env_slug(sc.environment_name) not in env_slugs:
                    errors.append(
                        f"{sc.scene_id}: environment_name "
                        f"{sc.environment_name!r} not in 'environments' list")

        if char_ids:
            known = set(char_ids)
            for seq in sequences:
                for sc in seq.scenes:
                    for c in sc.characters_present:
                        if c not in known:
                            errors.append(
                                f"{sc.scene_id}: unknown character {c!r} "
                                f"(expected one of {sorted(known)})")

        total_clips = 0
        for seq in sequences:
            for sc in seq.scenes:
                for cl in sc.clips:
                    total_clips += 1
                    if not cl.person and _action_has_person(cl.action):
                        errors.append(
                            f"{cl.clip_id}: person=false but action mentions "
                            f"a person: {cl.action!r}")

        if total_clips > max_clips:
            errors.append(
                f"total clip count {total_clips} exceeds max_clips={max_clips}; "
                f"consider tightening the story or increasing max_clips")
        return errors