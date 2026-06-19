"""XDS indexing/integration backend for the processing sub-pipeline."""

import freephil
from pathlib import Path

SCOPE_NAME = "xds"

PROGRAMS = {
    "xds": """\
xds
  .short_caption = XDS Parameters
  .help = Parameters for an XDS indexing/integration step.
{
  generate_inp = True
    .type = bool
    .short_caption = Generate XDS.INP
    .help = If True, generate a fresh XDS.INP from the raw data and run the \
            full XYCORR→IDXREF→INTEGRATE→CORRECT pipeline. If False, copy \
            XDS.INP and XPARM.XDS from the previous XDS step and run only \
            DEFPIX→INTEGRATE→CORRECT.
    .expert_level = 0
  on_indexing_failure = *auto retry reject bypass
    .type = choice
    .short_caption = On indexing failure
    .help = Action when XPARM.XDS is present but XDS_ASCII.HKL is absent \
            after the initial XDS run. auto: retry when no known symmetry is \
            supplied, bypass when known symmetry is supplied. retry: submit \
            forced integration job(s) (see retry_mode). reject: stop this \
            dataset immediately. bypass: re-run JOB= DEFPIX INTEGRATE CORRECT \
            in-place using the existing XPARM.XDS, space group unchanged.
    .expert_level = 1
  retry_mode = *auto p1 best top_ten
    .type = choice
    .short_caption = Forced integration retry mode
    .help = Ignored unless on_indexing_failure = retry. auto: use best \
            (top-scoring Bravais lattice). p1: one job with \
            SPACE_GROUP_NUMBER=1 (Pointless determines the true space group). \
            best: one job using the top-scoring Bravais lattice from IDXREF.LP. \
            top_ten: one SGE array job per starred Bravais lattice in IDXREF.LP \
            (legacy behavior).
    .expert_level = 1
  on_integration_failure = *reject bypass
    .type = choice
    .short_caption = On integration failure
    .help = Action when XPARM.XDS and INTEGRATE.HKL are present but \
            XDS_ASCII.HKL is absent (CORRECT failed), or when a subsequent \
            XDS run fails. reject: stop this dataset. bypass: log the \
            failure and continue to the next pipeline step.
    .expert_level = 1
  recycle_geometry = False
    .type = bool
    .short_caption = Recycle detector geometry
    .help = Recycle refined detector parameters (ROTATION_AXIS, ORGX, \
            DETECTOR_DISTANCE, etc.) extracted from the previous XDS step. \
            Only applied when generate_inp = False.
    .expert_level = 0
  update_symmetry = False
    .type = bool
    .short_caption = Update symmetry and resolution
    .help = Update SPACE_GROUP_NUMBER, UNIT_CELL_CONSTANTS, and \
            INCLUDE_RESOLUTION_RANGE from the most recent Aimless/Pointless \
            results before running. Only applied when generate_inp = False.
    .expert_level = 0
  skip_if_no_improvement = False
    .type = bool
    .short_caption = Skip if no improvement
    .help = Skip this step if the resolution limit has not improved over the \
            prior run. Only meaningful when update_symmetry = True.
    .expert_level = 1
  xds_inp = None
    .type = path
    .short_caption = XDS.INP file
    .help = Path to an existing XDS.INP file -- overrides all other xds.* \
            parameters at this step if provided.
    .expert_level = 0
}
""",
}

# Global (dataset-wide) settings for the XDS program.  Assembled into the
# processing scope as ``processing.xds`` by _build_processing_phil.  Swapping
# the indexing program = dropping in a backend that declares its own SETTINGS.
SETTINGS = """\
xds
  .short_caption = XDS settings
  .help = Dataset-wide XDS settings (apply to every XDS step).
{
  ncpu = 8
    .type = int
    .short_caption = CPUs per job
    .help = Number of CPUs allocated to each XDS SGE job.
    .expert_level = 0
  detector_edge = 0.8
    .type = float
    .short_caption = Detector edge fraction
    .help = Fraction of the detector radius used as the initial resolution limit.
    .expert_level = 1
  res_limit = cc12
    .type = str
    .short_caption = Resolution limit criterion
    .help = Criterion for the automatic high-resolution cutoff (cc12, i_over_sigma, or a multiplicity threshold).
    .expert_level = 0
  scaling = *correct aimless
    .type = choice
    .short_caption = Scaling source
    .help = Which XDS output Pointless/Aimless scale -- correct uses XDS_ASCII.HKL from CORRECT, aimless uses INTEGRATE.HKL.
    .expert_level = 1
}
"""


def run_step(step, ctx, rp, pipeline_subdir, forced):
    """Execute one XDS step.

    Selects the XDS scenario from PHIL booleans, checks for CC½ improvement
    when skip_if_no_improvement is set, and returns a status dict understood
    by ProcessingPipelineInterpreter.

    Context keys read:
      last_by_program["xds"]      — step_id of the previous XDS step
      last_by_program["aimless"]  — step_id of the previous Aimless step
      last_by_program["pointless"]— step_id of the previous Pointless step
      history_by_program["aimless"] — ordered list of completed Aimless step_ids

    Context keys written (via ctx_updates):
      _skip_n_steps — set to 2 when this step is intentionally skipped
                      (no CC½ improvement), telling the interpreter to skip
                      the next two steps (typically the paired pointless/aimless).
    """
    step_id  = step.id
    xds_p    = step.xds
    params   = rp.params
    prev_xds = ctx["last_by_program"].get("xds")
    prev_aim = ctx["last_by_program"].get("aimless")
    prev_ptl = ctx["last_by_program"].get("pointless")

    known_sym = params.get("known_sg", "none") != "none"
    on_indexing_failure = xds_p.on_indexing_failure
    if on_indexing_failure is freephil.Auto or on_indexing_failure == "auto":
        on_indexing_failure = "bypass" if known_sym else "retry"
    retry_mode = xds_p.retry_mode
    if retry_mode is freephil.Auto or retry_mode == "auto":
        retry_mode = "best"

    if xds_p.generate_inp:
        scenario = "xds_forced_integration" if forced else "xds_initial_indexing"
        rp._xds(step_id, params, scenario)

        if not rp._xds_ascii_ok(pipeline_subdir, step_id):
            step_dir      = Path(pipeline_subdir) / step_id
            xparm         = (step_dir / "XPARM.XDS").is_file()
            integrate_hkl = (step_dir / "INTEGRATE.HKL").is_file()
            err = rp._xds_error(pipeline_subdir, step_id) or "XDS did not produce XDS_ASCII.HKL"

            if not forced and xparm and not integrate_hkl:
                # IDXREF failure (or early-INTEGRATE failure before INTEGRATE.HKL is written)
                if on_indexing_failure == "retry":
                    return {
                        "status": "error",
                        "fatal": True,
                        "error_type": "needs_forced_integration",
                        "retry_mode": retry_mode,
                        "message": f"{step_id}: {err}",
                    }
                elif on_indexing_failure == "bypass":
                    rp._log(pipeline_subdir, "ERROR",
                            f"{step_id}: {err}")
                    rp._log(pipeline_subdir, "BYPASS",
                            f"{step_id}: re-running in-place: JOB= DEFPIX INTEGRATE CORRECT")
                    rp._xds(step_id, params, "xds_bypass_indexing_failure")
                    if rp._xds_ascii_ok(pipeline_subdir, step_id):
                        return {"status": "completed",
                                "message": f"{step_id}: completed (bypass)"}
                    err2 = (rp._xds_error(pipeline_subdir, step_id)
                            or "XDS did not produce XDS_ASCII.HKL after bypass")
                    return {"status": "error", "fatal": True,
                            "message": f"{step_id}: {err2}"}
                else:  # reject
                    return {"status": "error", "fatal": True, "message": f"{step_id}: {err}"}

            elif xparm and integrate_hkl:
                # CORRECT failure: on_integration_failure governs
                if xds_p.on_integration_failure == "bypass":
                    return {"status": "error", "fatal": False,
                            "message": f"{step_id}: {err} (bypassed)"}
                else:  # reject
                    return {"status": "error", "fatal": True, "message": f"{step_id}: {err}"}

            else:
                # No XPARM.XDS: hard stop regardless of params
                return {"status": "error", "fatal": True, "message": f"{step_id}: {err}"}

    else:
        if xds_p.skip_if_no_improvement and not forced:
            history = ctx["history_by_program"].get("aimless", [])
            if len(history) >= 2:
                def _cc12(log_path):
                    for line in Path(log_path).read_text().splitlines():
                        if "0.30: limit =" in line:
                            return line.split("=", 1)[1].split("A")[0].strip()
                    return "0.00"
                last, prev = history[-1], history[-2]
                res_last = _cc12(pipeline_subdir / last / f"{last}.log")
                res_prev = _cc12(pipeline_subdir / prev / f"{prev}.log")
                if float(res_last) <= float(res_prev):
                    return {
                        "status": "skipped",
                        "message": (f"{step_id}: skipped "
                                    f"(no CC½ improvement: {res_prev} → {res_last} Å)"),
                        "ctx_updates": {"_skip_n_steps": 2},
                    }

        if xds_p.update_symmetry:
            scenario = "xds_refine_symmetry"
        elif xds_p.recycle_geometry:
            scenario = "xds_recycle_detector"
        else:
            scenario = "xds_initial_indexing"

        rp._xds(step_id, params, scenario,
                prev_xds_id=prev_xds, prev_aimless_id=prev_aim, prev_pointless_id=prev_ptl)

        if not rp._xds_ascii_ok(pipeline_subdir, step_id):
            err = rp._xds_error(pipeline_subdir, step_id) or "XDS did not produce XDS_ASCII.HKL"
            return {"status": "error", "fatal": True, "message": f"{step_id}: {err}"}

    return {"status": "completed", "message": f"{step_id}: completed"}
