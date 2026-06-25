"""PHIL-driven pipeline interpreter for the analysis sub-pipeline (PanDDA).

Parallel to ``pandda_pipeline.PanDDAPreparer``, which is retained for
comparison.  Unlike the processing and refinement interpreters this one
runs directly in the CLI process (not on a compute node), so it takes
the PHIL params extract rather than a JSON params dict.

Backend interface
-----------------
Any module in ``avicenna.utils.phil.analysis`` that exports both
``PROGRAMS`` and ``run_step`` is discovered and registered.

  PROGRAMS : dict[str, str]
      PHIL scope strings (program parameter definitions).

  run_step(step, ctx) -> dict
      Execute one analysis step.  Return a dict with:

        status     : "completed" | "skipped" | "error"
        fatal      : bool  (only for "error")
        message    : str
        ctx_updates: dict  (optional)
"""

import importlib
import pkgutil
from pathlib import Path

from avicenna.pipelines.jobbase import SubPipelineInterpreter
from avicenna.utils.phil.analysis import AnalysisPHIL


# ---------------------------------------------------------------------------
# Backend discovery
# ---------------------------------------------------------------------------

def _build_registry() -> dict:
    """Return {program_name: run_step_callable} for all discovered backends."""
    import avicenna.utils.phil.analysis as _pkg
    pkg_dir = Path(_pkg.__file__).parent
    registry = {}
    for _finder, name, _ispkg in pkgutil.iter_modules([str(pkg_dir)]):
        if name.startswith("_"):
            continue
        mod = importlib.import_module(f"avicenna.utils.phil.analysis.{name}")
        if hasattr(mod, "PROGRAMS") and hasattr(mod, "run_step"):
            for prog_name in mod.PROGRAMS:
                registry[prog_name] = mod.run_step
    return registry


# ---------------------------------------------------------------------------
# Interpreter
# ---------------------------------------------------------------------------

class AnalysisPipelineInterpreter(SubPipelineInterpreter):
    """Iterate over the PHIL analysis step sequence and execute each step.

    A "just run it" sub-pipeline: PanDDA streams its own progress straight to the
    terminal (``STREAMS_PROGRAM_OUTPUT``); these status lines are emitted around
    it and also written to ``<output.directory>/analysis_pipeline.log``.

    Parameters
    ----------
    params : dict
        Optional keys:
          ``phil_string`` — PHIL text adopted on top of the default PHIL
                            before extracting the step list.
        All analysis configuration (sources, reference files, etc.) lives
        in the PHIL, not in this dict.
    """

    STREAMS_PROGRAM_OUTPUT = True

    def __init__(self, params: dict | None = None):
        self.params    = params or {}
        self._registry = _build_registry()
        self._log_dir  = None

    # -- logging + dispatch (see SubPipelineInterpreter) ---------------------

    def _log_path(self):
        return (self._log_dir / "analysis_pipeline.log") if self._log_dir else None

    def _invoke(self, step, ctx):
        return self._registry[step.program](step, ctx)

    def run(self):
        phil = AnalysisPHIL()
        if "phil_string" in self.params:
            phil.adopt_phil_string(self.params["phil_string"])
        self._log_dir = Path(str(phil._params.analysis.output.directory or "."))
        steps = phil._params.analysis.steps.step

        self._log("START", "analysis pipeline")

        ctx = {
            "last_by_program":    {},
            "history_by_program": {},
            "completed_steps":    set(),
            "failed_steps":       set(),
        }

        if not self._run_steps(steps, ctx):
            self._log("DONE", "analysis pipeline complete")
