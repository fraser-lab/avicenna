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
import json
import os
import pkgutil
import stat
from pathlib import Path

from avicenna.pipelines.processing_runner import RunProcess, format_aimless_summary
from avicenna.pipelines.jobbase import SubPipelineInterpreter
from avicenna.utils.info import OUTCOME_COMPLETED
from avicenna.utils.phil.processing import ProcessingPHIL
from avicenna.utils.software import suite_setup_sources
from avicenna.utils.utils import sg_lookup


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
# Orchestrator (writes worker scripts that call the PHIL interpreter)
# ---------------------------------------------------------------------------

class ProcessingOrchestrator(RunProcess):
    """RunProcess subclass whose worker scripts invoke ProcessingPipelineInterpreter.

    Inherits all outer orchestration (dataset discovery, params.json writing,
    SGE submission, forced-integration re-submission) from RunProcess. Overrides
    _write_worker_script (emit interpreter scripts) and _known_symmetry_entries
    (read symmetry from the PHIL known_symmetry scope rather than the legacy
    known_symmetry.txt / xds_input_parameters.txt files).

    Hung-job protection is no longer a per-script wall-clock alarm: every
    interpreter job runs under the progress-based stall watchdog in
    ``ProcessingPipelineInterpreter`` (see ``avicenna.pipelines.jobbase``).
    """

    def _known_symmetry_entries(self):
        """Build symmetry entries from the PHIL ``processing.known_symmetry``
        scope when enabled; otherwise fall back to the legacy file-based source.

        Each isoform becomes one entry (or two when refine_cell_integrate=both),
        matching the dict shape produced by the legacy path so the downstream
        loop and output-dir naming are unchanged.
        """
        phil = ProcessingPHIL()
        phil_string = self.params.get("phil_string")
        if phil_string:
            phil.adopt_phil_string(phil_string)
        ks = phil._params.processing.known_symmetry
        # ``isoform`` is .multiple, and the shipped default carries an empty
        # template instance (all data fields None); freephil APPENDS the user's
        # isoforms rather than replacing it.  Keep any isoform that carries real
        # content in ANY data field -- a space group, a unit cell, or a reference
        # HKL (which itself encodes the symmetry and cell).  ``id`` is ignored on
        # purpose: the template's default ``id = isoform_1`` would otherwise make
        # an otherwise-empty instance look populated.
        def _has_content(iso):
            return bool(iso.space_group or iso.unit_cell or iso.reference_hkl)
        isoforms = [iso for iso in ks.isoform if _has_content(iso)]
        if not (ks.enabled and isoforms):
            return super()._known_symmetry_entries()

        refcell = self.params.get("refine_cell_integrate", "true")
        entries = []
        for n, iso in enumerate(isoforms, start=1):
            sg = iso.space_group or "none"
            try:
                sg_number = str(sg_lookup(space_group=sg)[0]) if sg != "none" else "none"
            except ValueError:
                sg_number = "none"
            cell = " ".join(str(v) for v in iso.unit_cell) if iso.unit_cell else "none"
            base = {
                "crystal_number": str(n),
                "first_pass":     "none",
                "sg":             sg,
                "sg_number":      sg_number,
                "cell":           cell,
                "ref_hkl":        str(iso.reference_hkl) if iso.reference_hkl else "none",
                "ref_mtz":        "none",
            }
            if refcell == "both":
                entries.append({**base, "refine_cell_integrate": "true"})
                entries.append({**base, "refine_cell_integrate": "false"})
            else:
                entries.append({**base, "refine_cell_integrate": refcell})
        return entries

    def _write_worker_script(self, pipeline_subdir, per_params, method="run_single"):
        forced = (method == "run_single_forced")
        sh_sources, csh_sources = suite_setup_sources({
            "ccp4":   self.CCP4_BIN,
            "phenix": self.PHENIX_BIN,
        })
        _sh_extra  = "".join(f" && source {p}" for p in sh_sources)
        _csh_extra = "".join(f" && source {p}" for p in csh_sources)

        Path(pipeline_subdir, "params.json").write_text(
            json.dumps(per_params, indent=2, default=str)
        )

        # Every job (forced or not) runs under the interpreter's progress-based
        # stall watchdog (avicenna.pipelines.jobbase.SubPipelineJob), which kills
        # a job only after it stops writing files -- so a legitimately-long
        # integration or scratch copy is never killed, but a genuinely hung job
        # is.  No per-script wall-clock alarm is needed.
        _run_call = f"ProcessingPipelineInterpreter(params).run(forced={forced})\n"

        script = Path(pipeline_subdir) / "run_pipeline.py"
        script.write_text(f"""\
#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, {self._src_dir!r})
from avicenna.pipelines.processing import ProcessingPipelineInterpreter

shell = os.environ.get('SHELL', '/bin/bash')
if any(s in shell for s in ('csh', 'tcsh')):
    _sbgrid_cmd = "tcsh -c 'source /programs/sbgrid.cshrc{_csh_extra} && env'"
else:
    _sbgrid_cmd = "bash --norc --noprofile -c 'source /programs/sbgrid.shrc{_sh_extra} && env'"
_result = subprocess.run(_sbgrid_cmd, shell=True, capture_output=True, text=True)
for _line in _result.stdout.splitlines():
    if '=' in _line:
        _key, _, _val = _line.partition('=')
        if _key.isidentifier():
            os.environ[_key] = _val

params = json.loads(Path('params.json').read_text())
print("=" * 60)
print(f"pin          : {{params.get('pin', 'n/a')}}")
print(f"dataset      : {{params.get('pin_id', 'n/a')}}")
print(f"image_dir    : {{params.get('image_dir', 'n/a')}}")
print(f"image_range  : {{params.get('image_range', 'n/a')}}")
print(f"workdir      : {{params.get('pipeline_subdir', 'n/a')}}")
print(f"space_group  : {{params.get('sg', 'n/a')}}")
print(f"cell         : {{params.get('cell', 'n/a')}}")
print("=" * 60, flush=True)
{_run_call}""")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# Interpreter
# ---------------------------------------------------------------------------

class ProcessingPipelineInterpreter(SubPipelineInterpreter):
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
        self.rp        = rp if rp is not None else ProcessingOrchestrator(params)
        self._registry = _build_registry()

    # ------------------------------------------------------------------
    # Logging + dispatch + flow hooks (see SubPipelineInterpreter)
    # ------------------------------------------------------------------

    def _log_context(self):
        return self.params.get("pin_id")

    def _log_path(self):
        return Path(self.params["pipeline_subdir"]) / "pipeline.log"

    def _log_forced_dir(self):
        if self.params.get("force_integration") == "true":
            return Path(self.params["pipeline_subdir"]).name
        return None

    def _invoke(self, step, ctx):
        handler = self._registry[step.program]
        return handler(step, ctx, self.rp, ctx["pipeline_subdir"], ctx["forced"])

    def _pre_step(self, step, ctx):
        # Step-skip counter: a handler may ask to skip the next N steps.
        if ctx["_skip_n_steps"] > 0:
            ctx["_skip_n_steps"] -= 1
            self._log("SKIP", f"{step.id}: skipped (prior step did not improve)")
            ctx["failed_steps"].add(step.id)
            return True
        return False

    # ------------------------------------------------------------------
    # Per-process record (see avicenna.pipelines.jobbase)
    # ------------------------------------------------------------------

    def _record_directory(self):
        return self.params.get("pipeline_subdir")

    def _record_key(self):
        return self.params.get("pin_id")

    def _record_paths(self):
        """This job's dataset directory and crystal-form directory.

        Named as the result tree names them, so the sentinel can group records
        the same way results have always been grouped.  A forced-integration
        job sits one level deeper than an ordinary one, so the depth is derived
        rather than assumed.
        """
        tag = self.params.get("pin_id")
        try:
            dataset_dir = Path(self.rp._output_dir) / tag
            parts = Path(self.params["pipeline_subdir"]).relative_to(dataset_dir).parts
        except (AttributeError, KeyError, TypeError, ValueError):
            return None, None
        return str(dataset_dir), (str(dataset_dir / parts[0]) if parts else None)

    def _record_fields(self):
        params = self.params
        forced = params.get("force_integration") == "true"
        dataset_dir, crystal_form_dir = self._record_paths()
        fields = {
            "proc_path":       dataset_dir,
            "result_path":     crystal_form_dir,
            "pipeline_subdir": params.get("pipeline_subdir"),
            # The images as collected: image_dir is rewritten to the scratch
            # copy once the job starts, and that path outlives nothing.
            "image_dir":       params.get("original_image_dir") or params.get("image_dir"),
            "image_prefix":    params.get("image_prefix"),
            "image_range":     params.get("image_range"),
            "data_format":     params.get("data_format"),
            "forced":          forced,
            "known_sg":        params.get("known_sg"),
            "known_cell":      params.get("known_cell"),
            "refine_cell_integrate": params.get("refine_cell_integrate"),
            "ncpu":            params.get("ncpu"),
            "xds_jobs":        params.get("xds_jobs"),
            "job_id":          os.environ.get("JOB_ID"),
            "task_id":         os.environ.get("SGE_TASK_ID"),
        }
        if forced:
            # The symmetry this retry was told to force, which is what
            # distinguishes one retry of a dataset from another.
            fields["forced_sg"]   = params.get("sg")
            fields["forced_cell"] = params.get("cell")
        return fields

    def _final_mtz(self, ctx):
        """Absolute path of the merged data file refinement starts from, or None.

        Taken from the step that produced it, so the next stage reads the path
        from the record instead of rebuilding it from folder names.
        """
        last_rfree = ctx["last_by_program"].get("rfree")
        if not last_rfree:
            self._log("WARN", "no R-free step completed; final_mtz not recorded")
            return None
        mtz = Path(ctx["pipeline_subdir"]) / last_rfree / "XDS_ASCII_ctruncate.mtz"
        if not mtz.is_file():
            self._log("WARN", f"{mtz} not found; final_mtz not recorded")
            return None
        return str(mtz.resolve())

    def _on_fatal(self, step, ctx, result):
        if result.get("error_type") == "needs_forced_integration":
            ctx["triggered_forced"] = True
            self._log("FORCE", "submitting forced integration jobs")
            self.rp._run_forced_integration(
                self.params, ctx["pipeline_subdir"],
                retry_mode=result.get("retry_mode"))

    # ------------------------------------------------------------------
    # Stall-watchdog hooks (see avicenna.pipelines.jobbase)
    # ------------------------------------------------------------------

    def _watch_roots(self):
        """Watch the dataset working dir (XDS streams its .LP logs here) plus the
        scratch destination (written during the multi-minute image copy).  The
        scratch path may not exist when the watchdog starts -- it begins
        contributing once _setup_scratch creates it."""
        params = self.params
        roots = [params["pipeline_subdir"]]
        tmpdir = os.environ.get("TMPDIR")
        if tmpdir and params.get("data_format") != "hdf5":
            img = params.get("original_image_dir") or params.get("image_dir")
            if img:
                roots.append(os.path.join(tmpdir, Path(img).name))
        return roots

    def _stall_log_path(self):
        return Path(self.params["pipeline_subdir"]) / "pipeline.log"

    def _stall_context(self):
        return self.params.get("pin_id")

    def run(self, forced: bool = False):
        """Run the single-dataset pipeline under the stall watchdog."""
        with self._stall_watchdog():
            return self._run(forced=forced)

    def _run(self, forced: bool = False):
        """Run the sequential single-dataset pipeline.

        Parameters
        ----------
        forced : bool
            False -> normal indexing path (xds_initial_indexing).
            True  -> forced-integration path (xds_forced_integration).
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
        self._log("START",
                  f"{params.get('pin_id', '?')} | {label} | sg={sg} cell={cell}")

        # Opened before the scratch copy, which takes minutes on a big dataset
        # and is itself a place a job can die.
        self._open_record()

        self.rp._setup_scratch(params)

        ctx = {
            "last_by_program":    {},
            "history_by_program": {},
            "pipeline_subdir":    pipeline_subdir,
            "forced":             forced,
            "_skip_n_steps":      0,
            "triggered_forced":   False,
            "completed_steps":    set(),
            "failed_steps":       set(),
        }

        fatal_break = self._run_steps(steps, ctx)

        if fatal_break:
            # The record was closed on the failing step.  A handoff to forced
            # integration is added to it, so a dataset whose retries are still
            # queued is not read as one that simply stopped here.
            if ctx["triggered_forced"]:
                self._record_update(triggered_forced=True)
            return

        stats = {}
        last_aimless = ctx["last_by_program"].get("aimless")
        if last_aimless:
            stats = self.rp._aimless_stats(
                pipeline_subdir / last_aimless / f"{last_aimless}.log")
            self._log("DONE", format_aimless_summary(stats))
        final_mtz = self._final_mtz(ctx)
        if final_mtz:
            stats["final_mtz"] = final_mtz
        self._close_record(OUTCOME_COMPLETED, **stats)
