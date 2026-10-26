"""Abstract base + shared helpers for every pipeline stage.

`sentence_windows` was removed when Stage 2 stopped reading story.txt in
windows. Only `window_at` remains, used exclusively by Stage 1 (project)
to truncate the story intro.

Fragments-first flow: Stage 3a is now the ONLY stage that reads the whole
story. Every stage after it consumes fragments or per-clip inputs only.
"""
import base64
import hashlib
import json
import re
import string
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

from llm_client import LLMClient
from storage import StoryStorage
from templates import PROMPT_TEMPLATES, STYLE_PRESETS, VOCABULARIES

_PLACEHOLDERS = {"", "/", "-", "none", "null", "n/a"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def sig(obj) -> str:
    """Stable short hash used to detect stale stage outputs."""
    raw = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def tidy(value) -> str:
    if value is None:
        return ""
    s = str(value).strip().strip(".").strip()
    return "" if s.lower() in _PLACEHOLDERS else s


def render_sentences(templates: List[str], ctx: dict) -> str:
    """Render sentence templates. A sentence with ANY empty slot is dropped."""
    fmt = string.Formatter()
    out: List[str] = []
    for t in templates:
        fields = [f for _, f, _, _ in fmt.parse(t) if f]
        vals = {f: tidy(ctx.get(f)) for f in fields}
        if any(not v for v in vals.values()):
            continue
        s = re.sub(r"\s+", " ", t.format(**vals)).strip()
        if s:
            out.append(s[0].upper() + s[1:])
    return " ".join(out)


def iter_clips(scenes_data: dict) -> Iterator[Tuple[dict, dict]]:
    for scene in scenes_data.get("scenes", []):
        for clip in scene.get("clips", []):
            yield scene, clip


def load_png_as_data_uri(path: Path, max_mb: int = 4) -> str:
    """Return a data URI for a PNG. If larger than max_mb, downscale with Pillow."""
    path = Path(path)
    size = path.stat().st_size
    if size > max_mb * 1024 * 1024:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError(
                f"{path} is {size / 1024 / 1024:.1f}MB (> {max_mb}MB) "
                f"and Pillow is not installed. Either install Pillow or "
                f"pre-shrink the sheet."
            ) from exc
        import io
        img = Image.open(path)
        if max(img.size) > 1024:
            img.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


# ---------------------------------------------------------------------------
# Story windows (Stage 1 only)
# ---------------------------------------------------------------------------
def window_at(text: str, cursor: int, n_words: int) -> Tuple[str, int]:
    """Return (window text, absolute start offset) of up to n_words words."""
    tail = text[cursor:]
    ms = list(re.finditer(r"\S+", tail))
    if not ms:
        return "", cursor
    last = ms[min(n_words, len(ms)) - 1]
    return tail[ms[0].start():last.end()], cursor + ms[0].start()


# ---------------------------------------------------------------------------
# Base stage
# ---------------------------------------------------------------------------
class PipelineStage(ABC):
    def __init__(self, llm: LLMClient, storage: StoryStorage):
        self._llm = llm
        self._storage = storage

    @abstractmethod
    def run(self) -> None:
        raise NotImplementedError

    def _check_inputs(self, need_story: bool = True) -> bool:
        errors = self._storage.validate_inputs(need_story=need_story)
        if errors:
            for e in errors:
                print(f"Error: {e}")
            return False
        return True

    def _invoke_with_retry(self, chain, inputs,
                           prompts: Optional[Tuple[str, str]] = None,
                           max_retries: Optional[int] = None):
        """Retry chain.invoke(inputs).

        `inputs` is a dict for text-only prompts, or a list of messages for
        multimodal (vision) chains. When `prompts` is provided, the context-
        word warning is checked (text-only path).
        """
        cfg = self._llm._config
        if prompts and isinstance(inputs, dict):
            try:
                words = len((prompts[0] + "\n" + prompts[1].format(**inputs)).split())
                if words > cfg.max_context_words:
                    print(f"    warning: prompt is {words} words "
                          f"(limit {cfg.max_context_words})")
            except KeyError:
                pass
        retries = max_retries if max_retries is not None else cfg.llm_max_retries
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                return chain.invoke(inputs)
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    wait = 2 ** (attempt - 1)
                    print(f"    retry {attempt}/{retries} in {wait}s: {exc}")
                    time.sleep(wait)
                else:
                    raise
        raise last_exc

    # --- stale-output handling ----------------------------------------
    @staticmethod
    def merge_prior(base: dict, prior: dict, key: str, sig_name: str,
                    sig_fn: Callable[[dict, dict], str]) -> Tuple[int, int]:
        prior_idx = {c["clip_id"]: c for _, c in iter_clips(prior)}
        kept = stale = 0
        for scene, clip in iter_clips(base):
            now = sig_fn(scene, clip)
            meta = clip.setdefault("meta", {})
            old = prior_idx.get(clip["clip_id"])
            if old and old.get(key):
                if (old.get("meta") or {}).get(sig_name) == now:
                    clip[key] = old[key]
                    meta[sig_name] = now
                    kept += 1
                    continue
                stale += 1
            clip.pop(key, None)
            meta.pop(sig_name, None)
        return kept, stale

    @staticmethod
    def assemble_tree(scenes: list, assets: dict, project: dict) -> dict:
        return {
            "schema_version":   "3.0",
            "project":          project,
            "vocabularies":     VOCABULARIES,
            "prompt_templates": PROMPT_TEMPLATES,
            "style_presets":    STYLE_PRESETS,
            "assets":           assets,
            "scenes":           scenes,
        }

    # --- clip selection ------------------------------------------------
    def _clips_for_stage(
        self, scenes_data: Dict, done_fn: Callable[[Dict], bool],
    ) -> List[Tuple[int, Dict, Dict, bool]]:
        out: List[Tuple[int, Dict, Dict, bool]] = []
        n = 0
        for scene in scenes_data.get("scenes", []):
            for clip in scene.get("clips", []):
                n += 1
                out.append((n, scene, clip, done_fn(clip)))
        return out

    @staticmethod
    def _parse_clip_csv(spec: str, total: int) -> Set[int]:
        result: Set[int] = set()
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo_s, hi_s = part.split("-", 1)
                lo, hi = int(lo_s), int(hi_s)
                if lo < 1 or hi > total or lo > hi:
                    raise ValueError(f"range out of bounds: {part}")
                result.update(range(lo, hi + 1))
            else:
                n = int(part)
                if n < 1 or n > total:
                    raise ValueError(f"number out of bounds: {n}")
                result.add(n)
        return result

    def _select(self, clips, label: str, limit: Optional[int]) -> Optional[Set[int]]:
        if self._llm._config.interactive_clip_selection:
            selected = self._prompt_clip_selection(clips, label)
            if selected is None:
                return None
        else:
            selected = {n for n, _, _, done in clips if not done}
        if limit is not None:
            selected = set(sorted(selected)[:limit])
        return selected

    def _prompt_clip_selection(
        self,
        clips_with_status: List[Tuple[int, Dict, Dict, bool]],
        stage_label: str,
    ) -> Optional[Set[int]]:
        print("\n" + "=" * 60)
        print(f"Stage {stage_label} - Clip Selection")
        print("=" * 60)

        grouped: Dict[str, Tuple[Dict, List[Tuple[int, Dict, bool]]]] = {}
        for num, scene, clip, done in clips_with_status:
            sid = scene.get("scene_id", "")
            grouped.setdefault(sid, (scene, []))[1].append((num, clip, done))

        total = len(clips_with_status)
        pending = sum(1 for _, _, _, done in clips_with_status if not done)

        for sid, (scene, items) in grouped.items():
            n_pending = sum(1 for _, _, done in items if not done)
            print(f"\nScene {sid} - {scene.get('name', '')} "
                  f"({len(items)} clips, {n_pending} pending)")
            for num, clip, done in items:
                marker = "[DONE]" if done else "[    ]"
                print(f"  [{num:>2}] {marker} {clip.get('clip_id', '')} "
                      f"- {clip.get('name', '')}")

        print("\n" + "=" * 60)
        print(f"Total: {total} clips, {pending} pending")
        print("\nEnter CSV of clip numbers ('q' to quit):")
        print("  Examples: '1,3'  '1-3'  '1,3-4,5'  'all'  'pending'  'none'")

        while True:
            try:
                raw = input("> ").strip().lower()
            except EOFError:
                return {n for n, _, _, done in clips_with_status if not done}

            if raw == "q":
                return None
            if raw == "none":
                return set()
            if raw in ("", "pending"):
                return {n for n, _, _, done in clips_with_status if not done}
            if raw == "all":
                return {n for n, _, _, _ in clips_with_status}
            try:
                return self._parse_clip_csv(raw, total)
            except ValueError as e:
                print(f"  Error: {e}. Try again.")