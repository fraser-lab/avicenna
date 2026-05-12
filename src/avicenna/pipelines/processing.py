"""Program-agnostic pipeline interpreter for the data-processing sub-pipeline.

Replaces the hard-coded ``run_single`` / ``run_single_forced`` methods in
``proc_pipeline.RunProcess`` with a PHIL-driven loop.  ``proc_pipeline.py``
is retained in parallel for comparison and regression testing.

Backend interface
-----------------
Any module placed in ``avicenna.utils.phil.processing`` that exports both
``PROGRAMS`` and ``run_step`` is automatically discovered and registered.

  PROGRAMS : dict[str, str]
      PHIL scope strings, unchanged from the definition-only role.

  run_step(step, ctx, rp, pipeline_subdir, forced) -> dict
      Execute one pipeline step.  Return a dict with:

        status   : "completed" | "skipped" | "error"
        fatal    : bool  (only for "error"; stops the pipeline when True)
        message  : str   (logged to pipeline.log)
        error_type : str  (optional; "needs_forced_integration" triggers
                          a forced-integration SGE submission)
        ctx_updates : dict  (optional keys merged into ``ctx`` on success/skip)

Context dict keys (passed to every step handler)
-------------------------------------------------
  last_by_program    : {program: step_id}   last successful step per program
  history_by_program : {program: [step_ids]} ordered success list per program
  pipeline_subdir    : Path
  forced             : bool
  _skip_n_steps      : int  (set by a handler to skip the next N steps;
                             decremented by the interpreter each iteration)
"""

import importlib
import pkgutil
from pathlib import Path

from avicenna.pipelines.proc_pipeline import RunProcess
from avicenna.utils.phil.processing import ProcessingPHIL
from avicenna.utils.utils import QRefineWriter


# ---------------------------------------------------------------------------
# Backend discovery
# ---------------------------------------------------------------------------

def _build_registry() -> dict:
    """Return {program_name: run_step_callable} for all discovered backends."""
    import avicenna.utils.phil.processing as _pkg
    pkg_dir = Path(_pkg.__file__).parent
    registry = {}
    for _finder, name, _ispkg in pkgutil.iter_modules([str(pkg_dir)]):
        if name.startswith("_"):
            continue
        mod = importlib.import_module(f"avicenna.utils.phil.processing.{name}")
        if hasattr(mod, "PROGRAMS") and hasattr(mod, "run_step"):
            for prog_name in mod.PROGRAMS:
                registry[prog_name] = mod.run_step
    return registry


# ---------------------------------------------------------------------------
# Interpreter
# ---------------------------------------------------------------------------

class ProcessingPipelineInterpreter:
    """
    Iterate over the PHIL step sequence and execute each step via the
    registered backend handler.  No program names appear in this class;
    all program-specific logic lives in the backend modules.

    Parameters
    ----------
    params : dict
        Flat parameter dict (as written to params.json by the outer loop).
        If the key ``phil_string`` is present its value is adopted on top of
        the default PHIL before extracting the step list.
    rp : RunProcess or None
        Pre-built orchestrator.  A new one is constructed from ``params`` when
        None (the typical case on compute nodes).
    """

    def __init__(self, params, rp=None):
        self.params    = params
        self.rp        = rp if rp is not None else RunProcess(params)
        self._registry = _build_registry()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run(self, forced: bool = False):
        """Run the sequential single-dataset pipeline.

        Parameters
        ----------
        forced : bool
            False → normal indexing path (xds_initial_indexing).
            True  → forced-integration path (xds_forced_integration).
            The ``forced`` flag is passed verbatim to every step handler.
        """
        params          = self.params
        pipeline_subdir = Path(params["pipeline_subdir"])

        phil = ProcessingPHIL()
        if "phil_string" in params:
            phil.adopt_phil_string(params["phil_string"])
        steps = phil._params.processing.steps.step

        # Start log matches proc_pipeline.run_single / run_single_forced
        if forced:
            sg, cell = params.get("sg", "none"), params.get("cell", "none")
            label = pipeline_subdir.name
        else:
            sg, cell = params.get("known_sg", "none"), params.get("known_cell", "none")
            label = pipeline_subdir.parent.name
        self.rp._log(pipeline_subdir, "START",
                     f"{params.get('pin_id', '?')} | {label} | sg={sg} cell={cell}")

        self.rp._setup_scratch(params)

        ctx = {
            "last_by_program":    {},
            "history_by_program": {},
            "pipeline_subdir":    pipeline_subdir,
            "forced":             forced,
            "_skip_n_steps":      0,
            "completed_steps":    set(),
            "failed_steps":       set(),
        }

        fatal_break = False
        triggered_forced = False

        for step in steps:
            # Step-skip counter: set by a handler to skip the next N steps.
            if ctx["_skip_n_steps"] > 0:
                ctx["_skip_n_steps"] -= 1
                self.rp._log(pipeline_subdir, "RUN",
                             f"{step.id}: skipped (prior step did not improve)")
                ctx["failed_steps"].add(step.id)
                continue

            # Branching guards
            req_succeeded = step.run_if_succeeded
            req_failed    = step.run_if_failed
            if req_succeeded and req_succeeded not in ctx["completed_steps"]:
                self.rp._log(pipeline_subdir, "SKIP",
                             f"{step.id}: skipped ('{req_succeeded}' has not succeeded)")
                ctx["failed_steps"].add(step.id)
                continue
            if req_failed and req_failed not in ctx["failed_steps"]:
                self.rp._log(pipeline_subdir, "SKIP",
                             f"{step.id}: skipped ('{req_failed}' has not failed)")
                continue

            program = step.program
            handler = self._registry.get(program)
            if handler is None:
                self.rp._log(pipeline_subdir, "ERROR",
                             f"{step.id}: no backend registered for program '{program}'")
                fatal_break = True
                break

            result  = handler(step, ctx, self.rp, pipeline_subdir, forced)
            status  = result.get("status", "completed")
            message = result.get("message", f"{step.id}: {status}")

            if status == "completed":
                self.rp._log(pipeline_subdir, "RUN", message)
                ctx["last_by_program"][program] = step.id
                ctx["history_by_program"].setdefault(program, []).append(step.id)
                ctx.update(result.get("ctx_updates", {}))
                ctx["completed_steps"].add(step.id)

            elif status == "skipped":
                self.rp._log(pipeline_subdir, "RUN", message)
                ctx.update(result.get("ctx_updates", {}))
                ctx["failed_steps"].add(step.id)

            elif status == "error":
                self.rp._log(pipeline_subdir, "ERROR", message)
                ctx["failed_steps"].add(step.id)
                if result.get("fatal", False):
                    if result.get("error_type") == "needs_forced_integration":
                        triggered_forced = True
                        self.rp._log(pipeline_subdir, "FORCE",
                                     "submitting forced integration jobs")
                        self.rp._run_forced_integration(params, pipeline_subdir)
                    fatal_break = True
                    break
                # Non-fatal error: log and continue.

        if not fatal_break and not triggered_forced:
            last_aimless = ctx["last_by_program"].get("aimless")
            if last_aimless:
                summary = self.rp._aimless_summary(
                    pipeline_subdir / last_aimless / f"{last_aimless}.log")
                self.rp._log(pipeline_subdir, "DONE", summary)
            QRefineWriter(params["process_dir"]).write()
