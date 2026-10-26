"""util/qwen.py - export Qwen jobs from 7_qwen-prompts.json to 9_qwen-images.json.

Thin exporter. It never builds prompts: it only copies rendered_prompt.
Comfy owns image wiring (sheet -> first -> middle -> last).

β⁺: scenes → clips. No `segments` layer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import PipelineConfig  # noqa: E402


def build_export(data: dict) -> tuple[dict, int, int]:
    """Return (export dict, skipped_reused, missing_prompt)."""
    images, reused, missing = [], 0, 0
    for scene in data.get("scenes", []):
        for clip in scene.get("clips", []):
            for role in ("first", "middle", "last"):
                kf = (clip.get("keyframes") or {}).get(role)
                if not kf:
                    continue
                if kf.get("reuse_from"):
                    reused += 1
                    continue
                ig = kf.get("image_gen")
                if not ig:
                    continue
                if not ig.get("rendered_prompt"):
                    missing += 1
                    print(f"  warn: {kf.get('keyframe_id')} has no rendered_prompt")
                    continue
                images.append({
                    "sno":             len(images) + 1,
                    "keyframe_id":     kf["keyframe_id"],
                    "clip_id":         clip["clip_id"],
                    "scene":           scene.get("name", ""),
                    "clip":            clip.get("name", ""),
                    "frame":           role,
                    "template_id":     ig.get("template_id", ""),
                    "seed":            ig.get("seed"),
                    "positive_prompt": ig["rendered_prompt"],
                    "negative_prompt": ig.get("negative_prompt", ""),
                })
    return {"total_images": len(images), "images": images}, reused, missing


def export(cfg: PipelineConfig, source: str | None = None, output: str | None = None,
           allow_stale: bool = False) -> int:
    src = Path(source) if source else cfg.qwen_prompts_path
    if not src.exists():
        print(f"Error: {src} not found. Run stage 'qwen-prompts' first.")
        return 1
    if not allow_stale:
        for up in (cfg.motion_path, cfg.scenes_keyframes_path):
            if up.exists() and up.stat().st_mtime > src.stat().st_mtime:
                print(f"Error: {up.name} is newer than {src.name}. "
                      f"Run stage 'qwen-prompts' first (or use --allow-stale).")
                return 1
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Error: {src} is not valid JSON: {exc}")
        return 1
    if "scenes" not in data:
        print(f"Error: {src} is missing the 'scenes' key")
        return 1

    result, reused, missing = build_export(data)
    out = Path(output) if output else cfg.qwen_export_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {result['total_images']} image job(s) to {out} "
          f"({reused} reused frame(s) skipped, {missing} missing prompt(s))")
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export Qwen image jobs.")
    ap.add_argument("--input", default=None, help="Default: output/7_qwen-prompts.json")
    ap.add_argument("--output", default=None, help="Default: output/9_qwen-images.json")
    ap.add_argument("--allow-stale", action="store_true")
    args = ap.parse_args(argv)
    return export(PipelineConfig(), args.input, args.output, args.allow_stale)


if __name__ == "__main__":
    sys.exit(main())