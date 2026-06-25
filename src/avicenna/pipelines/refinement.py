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
  rfree_threshold          : float (stored by phaser for downstream checks)
  phaser_succeeded         : bool
  jelly_succeeded          : bool
  final_rfree              : float
"""

import importlib
import pathlib
import pkgutil
import sys

from avicenna.pipelines.jobbase import SubPipelineInterpreter
from avicenna.pipelines.legacy.qrefine_pipeline import QRefineWorker, RunQRefine
from avicenna.utils.phil.refine import RefinePHIL
from avicenna.utils.software import find_ccp4, find_phenix, suite_setup_sources


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

class RefinementPipelineInterpreter(SubPipelineInterpreter):
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

    # -- logging + dispatch (see SubPipelineInterpreter) ---------------------

    def _log_context(self):
        return self.params.get("job_id", self.params.get("pin", "?"))

    def _log_path(self):
        return self._job_dir() / "pipeline.log"

    def _invoke(self, step, ctx):
        handler = self._registry[step.program]
        return handler(step, ctx, self.params, self.params["suffix"],
                       self._job_dir())

    # ------------------------------------------------------------------
    # Stall-watchdog hooks (see avicenna.pipelines.jobbase).  Refinement
    # programs stream their logs to job_dir (see qrefine._run_cmd), so the
    # job dir's newest mtime is the progress signal.
    # ------------------------------------------------------------------

    def _job_dir(self):
        return pathlib.Path(self.params.get("job_dir", ".")).resolve()

    def _watch_roots(self):
        return [self._job_dir()]

    def _stall_log_path(self):
        return self._job_dir() / "pipeline.log"

    def _stall_context(self):
        return self.params.get("job_id", self.params.get("pin", "?"))

    def run(self):
        """Run the refinement step sequence under the stall watchdog."""
        with self._stall_watchdog():
            return self._run()

    def _run(self):
        params  = self.params
        suffix  = params["suffix"]

        phil = RefinePHIL()
        if "phil_string" in params:
            phil.adopt_phil_string(params["phil_string"])
        # Reference model comes from refine.phil (input scope), NOT the legacy
        # per-crystal qrefine_input_parameters.txt.  Inject so the program
        # handlers can keep reading params["mr_pdb"].  (The alternate/monomer
        # search model is phaser.alt_search_model, read from the step itself.)
        params["mr_pdb"] = phil._params.refine.input.reference_pdb
        steps = phil._params.refine.steps.step

        self._log("START", f"suffix={suffix}")

        ctx = {
            "last_by_program":    {},
            "history_by_program": {},
            "completed_steps":    set(),
            "failed_steps":       set(),
        }

        if not self._run_steps(steps, ctx):
            # A backend (the final refmac step) may stash a resolution + R-stats
            # summary for the completed job; append it when present.
            summary = ctx.get("final_summary")
            message = f"suffix={suffix} complete"
            if summary:
                message += f" — {summary}"
            self._log("DONE", message)


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
    list parsing, SGE submission, panDDA template) from RunQRefine.  Overrides
    ``_make_worker`` (emit interpreter scripts) and the input seams so MR
    settings come from refine.phil, NOT qrefine_input_parameters.txt.
    """

    def __init__(self, process_dir, dataset_list, params_file, data=None,
                 model=None, dry_run=False, fill=True, phil_string=None):
        super().__init__(process_dir, dataset_list, params_file, data=data,
                         model=model, dry_run=dry_run, fill=fill)
        self.phil_string = phil_string
        self.CCP4_BIN   = find_ccp4()[0]
        self.PHENIX_BIN = find_phenix()[0]
        # Resolve run-wide refine settings from refine.phil (not the legacy txt).
        phil = RefinePHIL()
        if phil_string:
            phil.adopt_phil_string(phil_string)
        self._water         = phil._params.refine.water
        self._reference_pdb = phil._params.refine.input.reference_pdb
        # Drive the -pe smp request from refine.ncpu (auto -> 1; REFMAC5 serial).
        self.ncpu           = phil._params.refine.ncpu

    # --- input seams: source from refine.phil, ignore qrefine_input_parameters.txt ---

    def _load_mr_params(self):
        return None  # PHIL mode does not read qrefine_input_parameters.txt

    def _validate_inputs(self, mr_params):
        if not self._reference_pdb:
            sys.exit("ERROR: refine.input.reference_pdb is not set — edit "
                     "refine.phil to point to your reference model.")
        if not pathlib.Path(self._reference_pdb).is_file():
            sys.exit(f"ERROR: reference_pdb not found: {self._reference_pdb}")

    def _dataset_params(self, dataset, mr_params):
        # Only water (drives hoh/nohoh suffix fan-out) is needed here; the
        # reference model is injected by the interpreter from phil_string.
        # Translate the PHIL vocabulary (both/hoh/nohoh) to the legacy water
        # values the shared worker's suffix logic expects (both/true/false).
        legacy_water = {"both": "both", "hoh": "true", "nohoh": "false"}[self._water]
        return {"mr_water": legacy_water}

    def _infer_pandda_suffix(self, mr_params):
        return "nohoh" if self._water == "nohoh" else "hoh"

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

    def _write_pandda_template(self, mr_params):
        """No-op on the PHIL path: the next stage is emitted as analysis.phil by
        RefinementSentinel on completion, not the legacy pandda_template.sh."""
        return
