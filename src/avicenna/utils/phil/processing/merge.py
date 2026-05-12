"""CCP4/Phenix merging-and-statistics backend for the processing sub-pipeline.

Step execution
--------------
run_step() handles all four programs by dispatching to private helpers.
Each helper pulls predecessor IDs from the context dict supplied by the
interpreter, so no program names are hardcoded in the interpreter itself.

Groups the four post-integration programs (Pointless, Aimless, freerflag/
CCP4 truncate, and Xtriage) that together constitute a single "merge and
evaluate" stage.  Each program appears as an individual step in the
pipeline sequence; this module contributes all four to the step schema.
"""

SCOPE_NAME = "merge"

PROGRAMS = {
    "pointless": """\
pointless
  .short_caption = Pointless Parameters
  .help = Parameters for the Pointless space-group determination step.
{
}
""",
    "aimless": """\
aimless
  .short_caption = Aimless Parameters
  .help = Parameters for the Aimless scaling and merging step.
{
  apply_res_cutoff = False
    .type = bool
    .short_caption = Apply resolution cutoff
    .help = If True, apply the CC1/2-based resolution cutoff during scaling.
    .expert_level = 0
}
""",
    "rfree": """\
rfree
  .short_caption = R-free Parameters
  .help = Parameters for the R-free flag generation step (freerflag / ctruncate).
{
}
""",
    "xtriage": """\
xtriage
  .short_caption = Xtriage Parameters
  .help = Parameters for the Phenix Xtriage data-quality analysis step.
{
}
""",
}


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

def _run_pointless(step, ctx, rp):
    rp._pointless(step.id, rp.params,
                  prev_xds_id=ctx["last_by_program"].get("xds"),
                  prev_aimless_id=ctx["last_by_program"].get("aimless"))
    return {"status": "completed", "message": f"{step.id}: completed"}


def _run_aimless(step, ctx, rp):
    rp._aimless(step.id, rp.params,
                prev_pointless_id=ctx["last_by_program"].get("pointless"),
                prev_aimless_id=ctx["last_by_program"].get("aimless"),
                apply_res_cutoff=step.aimless.apply_res_cutoff)
    return {"status": "completed", "message": f"{step.id}: completed"}


def _run_rfree(step, ctx, rp):
    rp._rfree(step.id, rp.params,
              prev_aimless_id=ctx["last_by_program"].get("aimless"))
    return {"status": "completed", "message": f"{step.id}: completed"}


def _run_xtriage(step, ctx, rp):
    rp._xtriage(step.id, rp.params,
                prev_xds_id=ctx["last_by_program"].get("xds"))
    return {"status": "completed", "message": f"{step.id}: completed"}


_HANDLERS = {
    "pointless": _run_pointless,
    "aimless":   _run_aimless,
    "rfree":     _run_rfree,
    "xtriage":   _run_xtriage,
}


def run_step(step, ctx, rp, pipeline_subdir, forced):
    """Execute one merging/statistics step (pointless, aimless, rfree, or xtriage)."""
    handler = _HANDLERS.get(step.program)
    if handler is None:
        return {
            "status": "error",
            "fatal": True,
            "message": f"{step.id}: merge module has no handler for '{step.program}'",
        }
    return handler(step, ctx, rp)
