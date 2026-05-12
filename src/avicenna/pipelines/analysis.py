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

class AnalysisPipelineInterpreter:
    """Iterate over the PHIL analysis step sequence and execute each step.

    Parameters
    ----------
    params : dict
        Optional keys:
          ``phil_string`` — PHIL text adopted on top of the default PHIL
                            before extracting the step list.
        All analysis configuration (sources, reference files, etc.) lives
        in the PHIL, not in this dict.
    """

    def __init__(self, params: dict | None = None):
        self.params    = params or {}
        self._registry = _build_registry()

    def run(self):
        phil = AnalysisPHIL()
        if "phil_string" in self.params:
            phil.adopt_phil_string(self.params["phil_string"])
        steps = phil._params.analysis.steps.step

        ctx = {
            "last_by_program":    {},
            "history_by_program": {},
            "completed_steps":    set(),
            "failed_steps":       set(),
        }

        for step in steps:
            program = step.program

            # Branching guards
            req_succeeded = step.run_if_succeeded
            req_failed    = step.run_if_failed
            if req_succeeded and req_succeeded not in ctx["completed_steps"]:
                print(f"SKIP  {step.id}: skipped ('{req_succeeded}' has not succeeded)",
                      flush=True)
                ctx["failed_steps"].add(step.id)
                continue
            if req_failed and req_failed not in ctx["failed_steps"]:
                print(f"SKIP  {step.id}: skipped ('{req_failed}' has not failed)",
                      flush=True)
                continue

            handler = self._registry.get(program)
            if handler is None:
                print(f"ERROR: {step.id}: no backend registered for '{program}'",
                      flush=True)
                break

            result  = handler(step, ctx)
            status  = result.get("status", "completed")
            message = result.get("message", f"{step.id}: {status}")

            if status == "completed":
                print(f"RUN   {step.id}: {message}", flush=True)
                ctx["last_by_program"][program] = step.id
                ctx["history_by_program"].setdefault(program, []).append(step.id)
                ctx.update(result.get("ctx_updates", {}))
                ctx["completed_steps"].add(step.id)

            elif status == "skipped":
                print(f"SKIP  {step.id}: {message}", flush=True)
                ctx["last_by_program"][program] = step.id
                ctx["history_by_program"].setdefault(program, []).append(step.id)
                ctx.update(result.get("ctx_updates", {}))
                ctx["failed_steps"].add(step.id)

            elif status == "error":
                print(f"ERROR {step.id}: {message}", flush=True)
                ctx["failed_steps"].add(step.id)
                if result.get("fatal", False):
                    break
