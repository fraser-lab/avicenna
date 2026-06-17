"""PHIL-driven pipeline interpreter for the structure-refinement sub-pipeline.

Parallel to ``qrefine_pipeline.RunQRefineJob``, which is retained for
comparison.  This interpreter replaces ``RunQRefineJob.run()`` on compute
nodes; the outer orchestration (dataset list, SGE submission) is unchanged.

Backend interface
-----------------
Any module in ``avicenna.utils.phil.refine`` that exports both ``PROGRAMS``
and ``run_step`` is discovered and registered.

  PROGRAMS : dict[str, str]
      PHIL scope strings (program parameter definitions).

  run_step(step, ctx, params, suffix, job_dir) -> dict
      Execute one pipeline step.  Return a dict with:

        status     : "completed" | "skipped" | "error"
        fatal      : bool  (only for "error"; stops the pipeline when True)
        message    : str   (logged to pipeline.log)
        ctx_updates: dict  (optional; merged into ctx on completed/skipped)

Context dict keys passed between handlers
-----------------------------------------
  rfree_after_rigid_body   : float
  jelly_input              : str   (path to PDB for the jelly step)
  mr_cutoff                : float (stored by phaser for downstream checks)
  phaser_succeeded         : bool
  jelly_succeeded          : bool
  final_rfree              : float
  phaser_monomer_succeeded : bool
  jelly_mono_input         : str
  jelly_mono_succeeded     : bool
"""

import importlib
import pathlib
import pkgutil

from avicenna.pipelines.legacy.qrefine_pipeline import QRefineWorker, RunQRefine
from avicenna.utils.phil.refine import RefinePHIL
from avicenna.utils.software import find_ccp4, find_phenix, suite_setup_sources
from avicenna.utils.utils import format_log_line


# ---------------------------------------------------------------------------
# Backend discovery
# ---------------------------------------------------------------------------

def _build_registry() -> dict:
    """Return {program_name: run_step_callable} for all discovered backends."""
    import avicenna.utils.phil.refine as _pkg
    pkg_dir = pathlib.Path(_pkg.__file__).parent
    registry = {}
    for _finder, name, _ispkg in pkgutil.iter_modules([str(pkg_dir)]):
        if name.startswith("_"):
            continue
        mod = importlib.import_module(f"avicenna.utils.phil.refine.{name}")
        if hasattr(mod, "PROGRAMS") and hasattr(mod, "run_step"):
            for prog_name in mod.PROGRAMS:
                registry[prog_name] = mod.run_step
    return registry


# ---------------------------------------------------------------------------
# Interpreter
# ---------------------------------------------------------------------------

class RefinementPipelineInterpreter:
    """Iterate over the PHIL refine step sequence and execute each step.

    Parameters
    ----------
    params : dict
        Per-suffix job parameters (as written to params_{suffix}.json).
        Must contain at minimum: ``mtz``, ``mr_pdb``, ``suffix``.
        Optionally: ``phil_string`` (adopted on top of default PHIL),
        ``job_dir`` (defaults to CWD), ``job_id``, ``context_width``.
    """

    def __init__(self, params: dict):
        self.params    = params
        self._registry = _build_registry()

    def _log(self, tag, message, job_dir):
        params = self.params
        job_id = params.get("job_id", params.get("pin", "?"))
        line   = format_log_line(tag, message, context=job_id,
                                 context_width=params.get("context_width"))
        with open(job_dir / "pipeline.log", "a") as fh:
            fh.write(line)
        print(line, end="", flush=True)

    def run(self):
        params  = self.params
        suffix  = params["suffix"]
        job_dir = pathlib.Path(params.get("job_dir", ".")).resolve()

        phil = RefinePHIL()
        if "phil_string" in params:
            phil.adopt_phil_string(params["phil_string"])
        steps = phil._params.refine.steps.step

        self._log("START", f"suffix={suffix}", job_dir)

        ctx = {
            "last_by_program":    {},
            "history_by_program": {},
            "completed_steps":    set(),
            "failed_steps":       set(),
        }

        fatal_break = False

        for step in steps:
            program = step.program

            # Branching guards
            req_succeeded = step.run_if_succeeded
            req_failed    = step.run_if_failed
            if req_succeeded and req_succeeded not in ctx["completed_steps"]:
                self._log("SKIP",
                          f"{step.id}: skipped ('{req_succeeded}' has not succeeded)",
                          job_dir)
                ctx["failed_steps"].add(step.id)
                continue
            if req_failed and req_failed not in ctx["failed_steps"]:
                self._log("SKIP",
                          f"{step.id}: skipped ('{req_failed}' has not failed)",
                          job_dir)
                continue

            handler = self._registry.get(program)
            if handler is None:
                self._log("ERROR",
                          f"{step.id}: no backend registered for '{program}'",
                          job_dir)
                fatal_break = True
                break

            result  = handler(step, ctx, params, suffix, job_dir)
            status  = result.get("status", "completed")
            message = result.get("message", f"{step.id}: {status}")

            if status == "completed":
                self._log("RUN", f"{step.id}: {message}", job_dir)
                ctx["last_by_program"][program] = step.id
                ctx["history_by_program"].setdefault(program, []).append(step.id)
                ctx.update(result.get("ctx_updates", {}))
                ctx["completed_steps"].add(step.id)

            elif status == "skipped":
                self._log("SKIP", f"{step.id}: {message}", job_dir)
                ctx["last_by_program"][program] = step.id
                ctx["history_by_program"].setdefault(program, []).append(step.id)
                ctx.update(result.get("ctx_updates", {}))
                ctx["failed_steps"].add(step.id)

            elif status == "error":
                self._log("ERROR", f"{step.id}: {message}", job_dir)
                ctx["failed_steps"].add(step.id)
                if result.get("fatal", False):
                    fatal_break = True
                    break
                # Non-fatal: log and continue

        if not fatal_break:
            self._log("DONE", f"suffix={suffix} complete", job_dir)


# ---------------------------------------------------------------------------
# Orchestrator (writes worker scripts that call the PHIL interpreter)
# ---------------------------------------------------------------------------

class _InterpreterWorker(QRefineWorker):
    """QRefineWorker whose generated scripts invoke RefinementPipelineInterpreter.

    Overrides the three hooks added to the base worker: it injects the user's
    ``phil_string`` into params, sources CCP4/Phenix setup scripts (for CLIBD
    and friends) after SBGrid, and swaps RunQRefineJob for the PHIL interpreter.
    """

    def __init__(self, dataset, mr_params, context_width=None, fill=True,
                 phil_string=None, sh_sources=(), csh_sources=()):
        super().__init__(dataset, mr_params, context_width=context_width, fill=fill)
        self.phil_string = phil_string
        self._sh_extra  = "".join(f" && source {p}" for p in sh_sources)
        self._csh_extra = "".join(f" && source {p}" for p in csh_sources)

    def _extra_params(self, suffix):
        return {"phil_string": self.phil_string} if self.phil_string is not None else {}

    def _sbgrid_block(self):
        return (
            "shell = os.environ.get('SHELL', '/bin/bash')\n"
            "if any(s in shell for s in ('csh', 'tcsh')):\n"
            f"    _sbgrid_cmd = \"tcsh -c 'source /programs/sbgrid.cshrc{self._csh_extra} && env'\"\n"
            "else:\n"
            f"    _sbgrid_cmd = \"bash --norc --noprofile -c 'source /programs/sbgrid.shrc{self._sh_extra} && env'\"\n"
        )

    def _worker_body(self, suffix):
        return (
            "from avicenna.pipelines.refinement import RefinementPipelineInterpreter\n"
            "RefinementPipelineInterpreter(params).run()\n"
        )


class RefinementOrchestrator(RunQRefine):
    """RunQRefine subclass whose worker scripts invoke the PHIL interpreter.

    Inherits all outer orchestration (CCP4 pinning, qrefine/ setup, dataset
    list parsing, SGE submission, panDDA template) from RunQRefine.  The only
    override is ``_make_worker``, which returns an ``_InterpreterWorker`` that
    emits scripts calling ``RefinementPipelineInterpreter(params).run()``.
    """

    def __init__(self, process_dir, dataset_list, params_file, data=None,
                 model=None, dry_run=False, fill=True, phil_string=None):
        super().__init__(process_dir, dataset_list, params_file, data=data,
                         model=model, dry_run=dry_run, fill=fill)
        self.phil_string = phil_string
        self.CCP4_BIN   = find_ccp4()[0]
        self.PHENIX_BIN = find_phenix()[0]

    def _make_worker(self, dataset, params, context_width):
        sh_sources, csh_sources = suite_setup_sources({
            "ccp4":   self.CCP4_BIN,
            "phenix": self.PHENIX_BIN,
        })
        return _InterpreterWorker(
            dataset, params, context_width=context_width, fill=self.fill,
            phil_string=self.phil_string,
            sh_sources=sh_sources, csh_sources=csh_sources,
        )
