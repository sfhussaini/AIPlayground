"""CLI entry point for the story pipeline."""
import argparse
import sys

from pipeline import StoryPipeline, PipelineConfig

STAGES = [
    "characters",          # 1.5  Vision → char_XX_sheet.json (review gate)
    "breakdown",           # 3a   whole story → story_fragmented.json
                           #      + libraries (envs, states, props) (review gate)
    "project",             # 1    style/color/title from first N words
    "scenes",              # 3b   build 3_scenes.json from fragments
                           #      (denormalizes libraries into scenes)
    "shots", "keyframes", "motion",
    "qwen-prompts", "ltx-prompts", "qwen-export",
    "validate", "check-character", "check-fragmented", "check-states",
    "all",
]


def parse_args():
    p = argparse.ArgumentParser(description="Story -> Video Data Flow Pipeline")
    p.add_argument("stage", choices=STAGES, help="Which stage to run.")
    p.add_argument("--force", action="store_true",
                   help="Regenerate the current stage's output. "
                        "For 'characters', re-extracts JSONs. "
                        "For 'scenes', rebuilds 3_scenes.json from existing "
                        "fragments (fragments are never touched). "
                        "For 'project', deletes its output. "
                        "For 'all', wipes outputs but keeps fragments.")
    p.add_argument("--re-fragment", action="store_true",
                   help="Stage 'breakdown': regenerate input/story_fragmented.json "
                        "from the story (destroys human edits + libraries).")
    p.add_argument("--refine", type=str, default=None, metavar="ID",
                   help="Stage 'breakdown': refine one scene or clip in the "
                        "fragmenter output (e.g. sq_01.sc_02 or sq_01.sc_02.c03). "
                        "Resets review.reviewed. Libraries are preserved; "
                        "edit story_fragmented.json to add/remove library entries.")
    p.add_argument("--all-clips", action="store_true",
                   help="Skip the clip selection prompt "
                        "(stages shots, keyframes, motion).")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N pending items.")
    args = p.parse_args()

    if args.re_fragment and args.refine:
        p.error("--re-fragment and --refine are mutually exclusive")
    if (args.re_fragment or args.refine) and args.stage != "breakdown":
        p.error("--re-fragment / --refine only apply to the 'breakdown' stage")
    if args.stage in ("shots", "keyframes", "motion") and args.force:
        p.error("--force is not applicable to filling stages; "
                "use clip selection to reprocess [DONE] clips.")
    return args


def main() -> int:
    args = parse_args()
    cfg = PipelineConfig()
    if args.all_clips or not sys.stdin.isatty():
        cfg.interactive_clip_selection = False

    pipeline = StoryPipeline(cfg)
    force = args.force

    if args.stage == "characters":
        pipeline.extract_characters(force=force)
    elif args.stage == "breakdown":
        if force:
            print("Note: --force has no effect on 'breakdown'. "
                  "Use --re-fragment to regenerate fragments, "
                  "or 'scenes --force' to rebuild 3_scenes.json.")
        pipeline.breakdown_story(
            re_fragment=args.re_fragment,
            refine_id=args.refine,
        )
    elif args.stage == "project":
        pipeline.generate_project(force=force)
    elif args.stage == "scenes":
        pipeline.build_scenes(force=force)
    elif args.stage == "shots":
        pipeline.fill_shots(limit=args.limit)
    elif args.stage == "keyframes":
        pipeline.fill_keyframes(limit=args.limit)
    elif args.stage == "motion":
        pipeline.fill_motion(limit=args.limit)
    elif args.stage == "qwen-prompts":
        pipeline.fill_qwen_prompts(force=force, limit=args.limit)
    elif args.stage == "ltx-prompts":
        pipeline.fill_ltx_prompts(force=force, limit=args.limit)
    elif args.stage == "qwen-export":
        return pipeline.export_qwen_images()
    elif args.stage == "validate":
        return 1 if pipeline.validate() else 0
    elif args.stage == "check-character":
        return 1 if pipeline.validate(["characters", "character"]) else 0
    elif args.stage == "check-fragmented":
        return 1 if pipeline.validate(["fragmented"]) else 0
    elif args.stage == "check-states":
        return 1 if pipeline.validate(["states"]) else 0
    elif args.stage == "all":
        pipeline.run_all(force=force)
    return 0


if __name__ == "__main__":
    sys.exit(main())