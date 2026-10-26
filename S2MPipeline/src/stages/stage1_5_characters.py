"""Stage 1.5 - extract character sheets from input/char_XX_sheet.png files.

One Vision LLM call per PNG. Output: input/char_XX_sheet.json conforming to
character.schema.json. Field semantics live entirely in models.py via
Field(description=...); the system message is only a 4-line policy glue.

The Vision LLM produces a STRICT SUBSET (CharExtractionLLM). Identifiers,
defaults, and human-authored variants are filled in here, by code:
  - character_id, sheet_file    <- filename
  - name                        <- placeholder from `subject`
  - identity_lock               <- compose_identity_lock()
  - wardrobe[*].wardrobe_id     <- wd_01, wd_02, ... (positional)
  - wardrobe[*].states          <- {"default": "worn as shown in the reference sheet"}
  - review.{source,reviewed}    <- 'vision', False

Iterative uncertainty resolution (per character):
  Step 0 - Filter result.uncertain_fields against real CharExtractionLLM
           fields. Hallucinated paths (e.g. 'name', 'character_id',
           'review.reviewed') are dropped and reported.
  Step A - For each remaining enum path (identity.* or 'subject'), re-ask
           the vision model with a FOCUSED question (character_field_retries
           attempts). Accept the answer only when confident=true.
  Step B - Otherwise, if interactive_character_review is True and stdin
           is a TTY, ask the user. Coerce the typed value to the schema type
           via pydantic TypeAdapter, and round-trip through
           CharExtractionLLM before accepting it.
  Step C - If the user skips, or we are non-interactive, the field stays
           in review.uncertain_fields.
"""
import json
import sys
import time
from typing import Any, List, Literal, Optional, get_args, get_origin

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, TypeAdapter, create_model

from models import (
    CharExtractionLLM, CharacterEntry, CharReview, CharWardrobe, CharIdentity,
)
from templates import CHARACTER_SYSTEM, CHARACTER_USER
from validate import compose_identity_lock
from .base import PipelineStage, load_png_as_data_uri


_DEFAULT_WARDROBE_STATE = "worn as shown in the reference sheet"


# ---------------------------------------------------------------------------
# Enum introspection + path helpers
# ---------------------------------------------------------------------------
def _literal_values(annotation) -> list[str]:
    """Return Literal[...] values from a field annotation, or [] if not an enum."""
    if get_origin(annotation) is Literal:
        return [str(v) for v in get_args(annotation)]
    for arg in get_args(annotation):
        if get_origin(arg) is Literal:
            return [str(v) for v in get_args(arg)]
    return []


def _enum_values(field_path: str) -> Optional[list[str]]:
    """Return the Literal values for a field path (e.g. 'eye_color',
    'hair.length', 'identity.eye_color', 'subject'). Returns None when the
    field is not an enum or the path cannot be resolved."""
    if field_path == "subject":
        values = _literal_values(CharExtractionLLM.model_fields["subject"].annotation)
        return values or None

    path = field_path
    if path.startswith("identity."):
        path = path[len("identity."):]
    parts = path.split(".")
    if not parts or parts == [""]:
        return None

    current: Any = CharIdentity
    for part in parts[:-1]:
        if not (isinstance(current, type) and issubclass(current, BaseModel)):
            return None
        if part not in current.model_fields:
            return None
        current = current.model_fields[part].annotation
        if not (isinstance(current, type) and issubclass(current, BaseModel)):
            return None

    leaf = parts[-1]
    if not (isinstance(current, type) and issubclass(current, BaseModel)):
        return None
    if leaf not in current.model_fields:
        return None

    values = _literal_values(current.model_fields[leaf].annotation)
    return values or None


def _full_path(field_path: str) -> str:
    """Map an uncertain-field path to its location in a CharExtractionLLM dump."""
    if field_path.startswith("identity."):
        return field_path
    if field_path == "subject" or field_path.startswith(("wardrobe.", "sheet_check.")):
        return field_path
    return f"identity.{field_path}"


def _nav(d, path: str):
    for part in path.split("."):
        if isinstance(d, list) and part.isdigit():
            d = d[int(part)]
        else:
            d = d[part]
    return d


def _set_path(d, path: str, value) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        if isinstance(d, list) and part.isdigit():
            d = d[int(part)]
        else:
            d = d[part]
    last = parts[-1]
    if isinstance(d, list) and last.isdigit():
        d[int(last)] = value
    else:
        d[last] = value


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------
def _annotation_for_path(full_path: str):
    """Return the resolved type annotation for a dotted path rooted at
    CharExtractionLLM. Raises ValueError if the path does not resolve."""
    parts = full_path.split(".")
    current: Any = CharExtractionLLM
    for part in parts:
        if isinstance(current, type) and issubclass(current, BaseModel):
            if part not in current.model_fields:
                raise ValueError(f"unknown field: {full_path}")
            current = current.model_fields[part].annotation
        else:
            if not part.isdigit():
                raise ValueError(f"expected list index in {full_path}")
            args = [a for a in get_args(current) if a is not type(None)]
            if not args:
                raise ValueError(f"cannot descend into {part!r} of {full_path}")
            current = args[0]
    return current


def _is_valid_field_path(field_path: str) -> bool:
    """True when field_path resolves to a real CharExtractionLLM field."""
    try:
        _annotation_for_path(_full_path(field_path))
        return True
    except ValueError:
        return False


def _split_uncertain(result: CharExtractionLLM) -> tuple[list[str], list[str]]:
    """Split result.uncertain_fields into (valid, hallucinated)."""
    valid, dropped = [], []
    for p in result.uncertain_fields:
        (valid if _is_valid_field_path(p) else dropped).append(p)
    return valid, dropped


# ---------------------------------------------------------------------------
# User input coercion (schema-aware)
# ---------------------------------------------------------------------------
def _coerce_for_path(full_path: str, raw: str):
    """Convert a user-typed string into the type expected at `full_path`.
    For List[...] fields, accepts either a JSON array or a comma-separated
    string. Raises ValueError on failure."""
    ann = _annotation_for_path(full_path)
    adapter = TypeAdapter(ann)

    if get_origin(ann) is list:
        if raw.strip().startswith("["):
            return adapter.validate_python(raw)
        items = [s.strip() for s in raw.split(",") if s.strip()]
        return adapter.validate_python(items)

    return adapter.validate_python(raw)


def _hint_for_path(full_path: str) -> str:
    """Short human-readable hint about what input the field accepts."""
    try:
        ann = _annotation_for_path(full_path)
    except ValueError:
        return "(free text)"
    if get_origin(ann) is list:
        return '(list: separate with commas, or JSON like ["a", "b"])'
    if ann is bool:
        return "(true/false, yes/no)"
    if ann is int:
        return "(integer)"
    if ann is float:
        return "(number)"
    return "(free text)"


def _validate_candidate(current: dict, full_path: str, value) -> Optional[str]:
    """Return None if replacing `full_path` with `value` yields a valid
    CharExtractionLLM; otherwise the error string."""
    test = dict(current)
    test.setdefault("uncertain_fields", [])
    try:
        _set_path(test, full_path, value)
        CharExtractionLLM(**test)
        return None
    except Exception as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------
class Stage1_5Characters(PipelineStage):
    def __init__(self, llm, storage, force: bool = False):
        super().__init__(llm, storage)
        self._force = force

    def run(self) -> None:
        st, cfg = self._storage, self._llm._config

        sheets = st.discover_character_sheets()
        if not sheets:
            print("No char_XX_sheet.png files found in input/.")
            return

        llm = self._llm.vision_structured(
            CharExtractionLLM, temperature=cfg.vision_temperature)

        t0 = time.perf_counter()
        ok = skipped = failed = 0

        for char_id, png in sheets.items():
            target = st.character_sheet_json_path(char_id)

            if target.exists() and not self._force:
                print(f"  {target.name} exists, skipping")
                skipped += 1
                continue

            try:
                img_uri = load_png_as_data_uri(png, cfg.vision_max_image_mb)
                messages = [
                    SystemMessage(content=CHARACTER_SYSTEM),
                    HumanMessage(content=[
                        {"type": "text",
                         "text": CHARACTER_USER.format(char_id=char_id)},
                        {"type": "image_url",
                         "image_url": {"url": img_uri}},
                    ]),
                ]
                result: CharExtractionLLM = self._invoke_with_retry(llm, messages)

                if cfg.resolve_uncertain_fields and result.uncertain_fields:
                    result = self._resolve_uncertain(char_id, img_uri, result)

                entry = self._assemble_entry(char_id, png.name, result)
                st.write_character_sheet_json(
                    char_id,
                    {"schema_version": "1.0",
                     "characters": [entry.model_dump()]},
                )
                n_unc = len(result.uncertain_fields)
                print(f"  wrote {target.name} "
                      f"(name placeholder: {entry.name!r}, "
                      f"{len(entry.wardrobe)} outfit(s), "
                      f"{n_unc} uncertain)")
                if n_unc:
                    print(f"    still uncertain: {', '.join(result.uncertain_fields)}")
                print(f"    review.reviewed = false - set to true after checking")
                ok += 1
            except Exception as exc:
                failed += 1
                print(f"  [{char_id}] error: {type(exc).__name__}: {exc}")

        print("\n" + "=" * 60)
        print(f"Stage 1.5 done in {time.perf_counter() - t0:.1f}s. "
              f"Extracted: {ok}  Skipped: {skipped}  Failed: {failed}")

    # ------------------------------------------------------------------
    # Uncertainty resolution
    # ------------------------------------------------------------------
    def _resolve_uncertain(self, char_id: str, img_uri: str,
                           result: CharExtractionLLM) -> CharExtractionLLM:
        cfg = self._llm._config

        # --- Step 0: drop hallucinated paths -------------------------
        valid, dropped = _split_uncertain(result)
        if dropped:
            print(f"\n  [{char_id}] dropping {len(dropped)} field(s) the model "
                  f"invented (not on CharExtractionLLM): "
                  f"{', '.join(dropped)}")
        if not valid:
            # nothing real to resolve -> strip the invented names
            result = result.model_copy(update={"uncertain_fields": []})
            return result

        print(f"\n  [{char_id}] resolving "
              f"{len(valid)} uncertain field(s): "
              f"{', '.join(valid)}")

        current = result.model_dump()
        current.pop("uncertain_fields", None)
        remaining: list[str] = []
        skip_remaining = False
        interactive = (cfg.interactive_character_review
                       and sys.stdin.isatty())

        for field_path in valid:
            if skip_remaining:
                remaining.append(field_path)
                continue

            # --- Step A: focused vision re-check (enum fields only) ---
            values = _enum_values(field_path)
            if values is not None:
                resolved = self._focused_vision_check(
                    img_uri, field_path, values, cfg.character_field_retries)
                if resolved is not None:
                    _set_path(current, _full_path(field_path), resolved)
                    print(f"      v {field_path} -> {resolved} (vision re-check)")
                    continue

            # --- Step B: ask the user ---
            if interactive:
                action, value = self._ask_user(
                    char_id, field_path, values, current)
                if action == "quit":
                    skip_remaining = True
                    remaining.append(field_path)
                    continue
                if action == "set":
                    _set_path(current, _full_path(field_path), value)
                    print(f"      v {field_path} -> {value!r} (user)")
                    continue
                # action == "skip" -> fall through

            remaining.append(field_path)

        current["uncertain_fields"] = remaining
        try:
            return CharExtractionLLM(**current)
        except Exception as exc:
            print(f"      warning: rebuilt result failed validation ({exc}); "
                  f"reverting to vision-only values for this character")
            return result.model_copy(update={"uncertain_fields": remaining})

    def _focused_vision_check(self, img_uri: str, field_path: str,
                              values: list[str], retries: int):
        """Re-ask the vision model, focused on one field.
        Returns the confident value, or None if never confident."""
        FocusedCheck = create_model(
            "FocusedCheck",
            value=(Literal[tuple(values)],
                   Field(description="Best guess for this field.")),
            confident=(bool,
                       Field(description="True only if the picture clearly shows it.")),
        )
        llm = self._llm.vision_structured(FocusedCheck, temperature=0.1)
        leaf = field_path.rsplit(".", 1)[-1].replace("_", " ")
        question = (
            f"Look carefully at the picture and answer only this question.\n"
            f"FIELD: {leaf}\n"
            f"Pick exactly one of: {', '.join(values)}\n"
            f"Set confident=true only if you can actually see it."
        )
        messages = [
            SystemMessage(content=(
                "You answer one question about a picture. "
                "Be honest about confidence."
            )),
            HumanMessage(content=[
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": img_uri}},
            ]),
        ]
        for attempt in range(1, retries + 1):
            try:
                check = self._invoke_with_retry(llm, messages, max_retries=1)
                if check.confident:
                    return check.value
            except Exception as exc:
                print(f"        focused check on {field_path} "
                      f"attempt {attempt} failed: {exc}")
        return None

    def _ask_user(self, char_id: str, field_path: str,
                  values: Optional[list[str]], current: dict
                  ) -> tuple[str, Any]:
        """Returns ('set', value) | ('skip', None) | ('quit', None)."""
        full = _full_path(field_path)
        try:
            cur_val = _nav(current, full)
        except (KeyError, IndexError, TypeError):
            cur_val = None

        print(f"\n  [{char_id}] Could not determine '{field_path}' from the sheet.")
        if values:
            print(f"      options : {', '.join(values)}")
        else:
            print(f"      {_hint_for_path(full)}")
        print(f"      current : {cur_val!r}")
        print(f"      Enter a value, 's' to skip, or 'q' to skip all remaining.")

        while True:
            try:
                raw = input("      > ").strip()
            except EOFError:
                return ("skip", None)
            low = raw.lower()
            if low in ("q", "quit"):
                return ("quit", None)
            if low in ("s", "skip", ""):
                return ("skip", None)

            if values is not None:
                if raw in values:
                    return ("set", raw)
                print(f"      Invalid. Must be one of: {', '.join(values)}")
                continue

            try:
                value = _coerce_for_path(full, raw)
            except (ValueError, TypeError) as exc:
                print(f"      Invalid: {exc}. Try again.")
                continue

            err = _validate_candidate(current, full, value)
            if err:
                print(f"      Invalid: {err}. Try again.")
                continue
            return ("set", value)

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------
    @staticmethod
    def _assemble_entry(char_id: str, sheet_name: str,
                        v: CharExtractionLLM) -> CharacterEntry:
        lock_input = {
            "subject":  v.subject,
            "identity": v.identity.model_dump(),
        }
        lock = compose_identity_lock(lock_input)

        wardrobe = [
            CharWardrobe(
                wardrobe_id=f"wd_{i:02d}",
                text=w.text,
                garments=w.garments,
                footwear=w.footwear,
                states={"default": _DEFAULT_WARDROBE_STATE},
            )
            for i, w in enumerate(v.wardrobe, start=1)
        ]

        return CharacterEntry(
            character_id=char_id,
            name=v.subject.capitalize(),
            subject=v.subject,
            sheet_file=sheet_name,
            identity=v.identity,
            identity_lock=lock,
            wardrobe=wardrobe,
            sheet_check=v.sheet_check,
            review=CharReview(source="vision",
                              reviewed=False,
                              uncertain_fields=v.uncertain_fields),
        )