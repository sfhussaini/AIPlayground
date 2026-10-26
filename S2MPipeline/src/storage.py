"""All filesystem access for the pipeline (I/O isolation).

Fragments-first, no 2_assets.json: character data comes from
input/char_XX_sheet.json; per-story libraries (environments,
wardrobe_states, props) come from input/story_fragmented.json and are
denormalized into output/3_scenes.json by Stage 3b.
"""
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from config import PipelineConfig


class StoryStorage:
    def __init__(self, config: PipelineConfig):
        self._config = config

    # --- directories / atomic write -----------------------------------
    def ensure_output_dir(self) -> None:
        Path(self._config.output_dir).mkdir(parents=True, exist_ok=True)

    def ensure_tmp_dir(self) -> Path:
        p = Path(self._config.output_dir) / "tmp"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _atomic_write(self, final_path: Path, content: str) -> None:
        tmp_dir = self.ensure_tmp_dir()
        tmp_path = tmp_dir / f"{final_path.name}.{os.getpid()}.tmp"
        try:
            tmp_path.write_text(content, encoding="utf-8")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, final_path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    # --- inputs --------------------------------------------------------
    def validate_inputs(self, need_story: bool = True) -> List[str]:
        errors: List[str] = []
        if need_story:
            if not self._config.story_path.exists():
                errors.append(f"Missing story file: {self._config.story_path}")
            elif not self._config.story_path.read_text(encoding="utf-8").strip():
                errors.append(f"Story file is empty: {self._config.story_path}")
        return errors

    def read_story(self) -> str:
        return self._config.story_path.read_text(encoding="utf-8")

    def story_sha1(self) -> str:
        return hashlib.sha1(self.read_story().encode("utf-8")).hexdigest()

    def discover_character_sheets(self) -> Dict[str, Path]:
        pattern = re.compile(r"^(char_\d{2})_sheet\.png$", re.IGNORECASE)
        sheets: Dict[str, Path] = {}
        input_dir = self._config.input_path
        if not input_dir.exists():
            return sheets
        for f in sorted(input_dir.iterdir()):
            m = pattern.match(f.name)
            if m:
                sheets[m.group(1)] = f
        return sheets

    # --- fragmented story (input/story_fragmented.json) ---------------
    def fragmented_exists(self) -> bool:
        return self._config.fragmented_path.exists()

    def read_fragmented(self) -> Dict[str, Any]:
        return self._read_json(self._config.fragmented_path)

    def write_fragmented(self, d: Dict[str, Any]) -> None:
        self._write_json(self._config.fragmented_path, d)

    def delete_fragmented(self) -> None:
        self._config.fragmented_path.unlink(missing_ok=True)

    # --- legacy single-file character.json ----------------------------
    def character_exists(self) -> bool:
        return self._config.character_path.exists()

    def read_character_raw(self) -> Dict[str, Any]:
        return json.loads(self._config.character_path.read_text(encoding="utf-8"))

    # --- per-sheet character JSONs ------------------------------------
    def character_sheet_json_path(self, char_id: str) -> Path:
        return self._config.input_path / f"{char_id}_sheet.json"

    def character_sheet_json_exists(self, char_id: str) -> bool:
        return self.character_sheet_json_path(char_id).exists()

    def read_character_sheet_json(self, char_id: str) -> Dict[str, Any]:
        return self._read_json(self.character_sheet_json_path(char_id))

    def write_character_sheet_json(self, char_id: str, d: Dict[str, Any]) -> None:
        self._write_json(self.character_sheet_json_path(char_id), d)

    def delete_character_sheet_json(self, char_id: str) -> None:
        self.character_sheet_json_path(char_id).unlink(missing_ok=True)

    def iter_character_sheet_jsons(self) -> Iterator[tuple[str, Optional[Dict[str, Any]]]]:
        """Yield (char_id, parsed-or-None). None means the file exists but is invalid."""
        pattern = re.compile(r"^(char_\d{2})_sheet\.json$", re.IGNORECASE)
        input_dir = self._config.input_path
        if not input_dir.exists():
            return
        for f in sorted(input_dir.iterdir()):
            m = pattern.match(f.name)
            if not m:
                continue
            try:
                yield m.group(1), self._read_json(f)
            except Exception:
                yield m.group(1), None

    # --- freshness ----------------------------------------------------
    @staticmethod
    def is_newer(a: Path, b: Path) -> bool:
        return a.exists() and b.exists() and a.stat().st_mtime > b.stat().st_mtime

    # --- json helpers -------------------------------------------------
    def _read_json(self, path: Path) -> Dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, data: Dict[str, Any]) -> None:
        self._atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))

    # --- 1 project ----------------------------------------------------
    def project_exists(self) -> bool:              return self._config.project_path.exists()
    def read_project(self)  -> Dict[str, Any]:     return self._read_json(self._config.project_path)
    def write_project(self, d: Dict[str, Any]):    self._write_json(self._config.project_path, d)
    def delete_project(self) -> None:              self._config.project_path.unlink(missing_ok=True)

    # --- 3 scenes (json) ----------------------------------------------
    def scenes_exists(self) -> bool:               return self._config.scenes_path.exists()
    def read_scenes(self)   -> Dict[str, Any]:     return self._read_json(self._config.scenes_path)
    def write_scenes(self, d: Dict[str, Any]):     self._write_json(self._config.scenes_path, d)
    def delete_scenes(self) -> None:               self._config.scenes_path.unlink(missing_ok=True)

    # --- 4 / 5 / 6 ----------------------------------------------------
    def scenes_shots_exists(self) -> bool:           return self._config.scenes_shots_path.exists()
    def read_scenes_shots(self)   -> Dict[str, Any]: return self._read_json(self._config.scenes_shots_path)
    def write_scenes_shots(self, d: Dict[str, Any]): self._write_json(self._config.scenes_shots_path, d)

    def scenes_keyframes_exists(self) -> bool:       return self._config.scenes_keyframes_path.exists()
    def read_scenes_keyframes(self) -> Dict[str, Any]: return self._read_json(self._config.scenes_keyframes_path)
    def write_scenes_keyframes(self, d: Dict[str, Any]): self._write_json(self._config.scenes_keyframes_path, d)

    def motion_exists(self) -> bool:               return self._config.motion_path.exists()
    def read_motion(self)   -> Dict[str, Any]:     return self._read_json(self._config.motion_path)
    def write_motion(self, d: Dict[str, Any]):     self._write_json(self._config.motion_path, d)

    # --- 7 / 8 / 9 ----------------------------------------------------
    def qwen_prompts_exists(self) -> bool:         return self._config.qwen_prompts_path.exists()
    def read_qwen_prompts(self)   -> Dict[str, Any]: return self._read_json(self._config.qwen_prompts_path)
    def write_qwen_prompts(self, d: Dict[str, Any]): self._write_json(self._config.qwen_prompts_path, d)

    def ltx_prompts_exists(self) -> bool:          return self._config.ltx_prompts_path.exists()
    def read_ltx_prompts(self)   -> Dict[str, Any]: return self._read_json(self._config.ltx_prompts_path)
    def write_ltx_prompts(self, d: Dict[str, Any]): self._write_json(self._config.ltx_prompts_path, d)

    def write_qwen_export(self, d: Dict[str, Any]): self._write_json(self._config.qwen_export_path, d)

    # --- traversal ----------------------------------------------------
    def iter_clips(self, scenes_data: Dict[str, Any]) -> Iterator[tuple[dict, dict]]:
        """scenes -> clips. No segments layer."""
        for scene in scenes_data.get("scenes", []):
            for clip in scene.get("clips", []):
                yield scene, clip