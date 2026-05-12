"""XDS indexing/integration backend for the processing sub-pipeline."""

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
  ncpu = 8
    .type = int
    .short_caption = CPUs per job
    .help = Number of CPUs allocated to this XDS SGE job.
    .expert_level = 1
  xds_inp = None
    .type = path
    .short_caption = XDS.INP file
    .help = Path to an existing XDS.INP file -- overrides all other xds.* \
            parameters at this step if provided.
    .expert_level = 0
  detector_edge = 0.8
    .type = float
    .short_caption = Detector edge fraction
    .help = Fraction of the detector radius used as the initial resolution limit.
    .expert_level = 1
  indexing_images = first_half
    .type = str
    .short_caption = Indexing images
    .help = Which images to use for indexing (first_half, all, or a range).
    .expert_level = 1
  try_alt_sym = all
    .type = str
    .short_caption = Try alternative symmetry
    .help = Whether to attempt alternative space groups after indexing \
            (all, none, or a comma-separated list of lattice types).
    .expert_level = 1
  no_symmetry = False
    .type = bool
    .short_caption = Skip symmetry determination
    .help = If True, skip Pointless space-group determination and use only \
            the XDS-determined symmetry.
    .expert_level = 2
  res_limit = cc12
    .type = str
    .short_caption = Resolution limit criterion
    .help = Criterion for automatic high-resolution cutoff (cc12 or i_over_sigma).
    .expert_level = 1
  cleanup = partial
    .type = str
    .short_caption = Cleanup level
    .help = How much intermediate output to remove after processing \
            (none, partial, or full).
    .expert_level = 2
  refine_cell_integrate = both
    .type = str
    .short_caption = Cell refinement mode
    .help = Whether to refine the unit cell during INTEGRATE (true, false, or both).
    .expert_level = 2
  ref_hkl = None
    .type = path
    .short_caption = Reference HKL
    .help = Path to a reference HKL file for reindexing (optional).
    .expert_level = 2
  ref_mtz = None
    .type = path
    .short_caption = Reference MTZ
    .help = Path to a reference MTZ file for reindexing (optional).
    .expert_level = 2
}
""",
}


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

    if xds_p.generate_inp:
        scenario = "xds_forced_integration" if forced else "xds_initial_indexing"
        rp._xds(step_id, params, scenario)

        if not rp._xds_ascii_ok(pipeline_subdir, step_id):
            err = rp._xds_error(pipeline_subdir, step_id) or "XDS did not produce XDS_ASCII.HKL"
            if not forced and (Path(pipeline_subdir) / step_id / "XPARM.XDS").is_file():
                return {
                    "status": "error",
                    "fatal": True,
                    "error_type": "needs_forced_integration",
                    "message": f"{step_id}: {err}",
                }
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
