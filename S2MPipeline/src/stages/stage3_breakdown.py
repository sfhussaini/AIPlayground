"""Stage 3 - dispatcher for Stage 3a (fragment, LLM).

Stage 3b (build_scenes) is now a SEPARATE CLI command (`python main.py scenes`)
because it needs `2_assets.json` to already exist, and the fragmenter runs
BEFORE assets in the new flow. This dispatcher therefore only handles
fragmentation.

--re-fragment  delete fragments and redo Stage 3a (destroys edits).
--refine <id>  redo one scene or clip (resets review.reviewed).
--re-fragment and --refine are mutually exclusive.
"""
from .base import PipelineStage
from .stage3a_fragment import Stage3aFragment


class Stage3Breakdown(PipelineStage):
    def __init__(self, llm, storage,
                 re_fragment: bool = False,
                 refine_id: str | None = None):
        super().__init__(llm, storage)
        self._re_fragment = re_fragment
        self._refine_id = refine_id

    def run(self) -> None:
        if self._re_fragment and self._refine_id:
            print("Error: --re-fragment and --refine are mutually exclusive.")
            return

        Stage3aFragment(
            self._llm, self._storage,
            re_fragment=self._re_fragment,
            refine_id=self._refine_id,
        ).run()