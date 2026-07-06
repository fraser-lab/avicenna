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

import os
import subprocess
from pathlib import Path

SCOPE_NAME = "merge"

PROGRAMS = {
    "pointless": """\
pointless
  .short_caption = Pointless Parameters
  .help = Parameters for the Pointless space-group determination step.
{
  enforce_spacegroup = False
    .type = bool
    .short_caption = Enforce space group
    .help = If True, enforce the space group from the previous Aimless run \
            (or from the pipeline known_sg on the first pass). REINDEX is \
            always included so reference-group transitions are allowed. Set \
            to True only after XDS has already integrated in the target \
            symmetry (i.e. on the step that follows 7_xds).
    .expert_level = 1
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
# Error helpers
# ---------------------------------------------------------------------------

def _read_log_error(log_path: Path) -> str:
    """Return the first error line from a CCP4 log, or a fallback message."""
    try:
        lines = log_path.read_text().splitlines()
        for i, line in enumerate(lines):
            if "**** ERROR ****" in line or "**** CCP4 SEVERE ERROR ****" in line:
                for next_line in lines[i + 1:]:
                    if next_line.strip():
                        return next_line.strip()
        return "see log for details"
    except OSError:
        return "log file not found"


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

def _run_pointless(step, ctx, rp, pipeline_subdir):
    step_id = step.id
    prev_xds_id     = ctx["last_by_program"].get("xds")
    prev_aimless_id = ctx["last_by_program"].get("aimless")

    if prev_xds_id is None:
        return {"status": "error", "fatal": True,
                "message": f"no prior XDS step succeeded"}

    job_dir = pipeline_subdir / step_id
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(job_dir)

    pointless = rp.CCP4_BIN / "pointless"
    scaling   = rp.params.get("scaling", "correct")
    known_sg  = rp.params.get("known_sg", "none")

    pointless_input = "INTEGRATE.HKL" if scaling == "aimless" else "XDS_ASCII.HKL"
    hklin    = f"../{prev_xds_id}/{pointless_input}"
    hklout   = f"XDS_ASCII_{step_id}.mtz"
    log_path = job_dir / f"{step_id}.log"

    # Determine whether to enforce a specific space group
    sg = None
    if step.pointless.enforce_spacegroup:
        if prev_aimless_id:
            aimless_log = pipeline_subdir / prev_aimless_id / f"{prev_aimless_id}.log"
            aim_text = aimless_log.read_text()
            sg = next(
                (line.split(":", 1)[1].replace(" ", "").strip()
                 for line in aim_text.splitlines() if "Space group:" in line),
                None,
            )
        elif known_sg != "none":
            sg = known_sg

    stdin_lines = [
        f"HKLIN {hklin}",
        f"HKLOUT {hklout}",
        "SETTING SYMMETRY-BASED",
    ]
    if sg:
        # REINDEX is required when the input's reference group differs from
        # the target SG (e.g. P 4 → P41212).  The identity operator h,k,l
        # lets Pointless recheck the data without actually changing indices.
        stdin_lines.append(f"SPACEGROUP {sg}")
        stdin_lines.append("REINDEX h,k,l")
    stdin_lines.append("END")
    stdin = "\n".join(stdin_lines) + "\n"

    with open(log_path, "w") as fh:
        subprocess.run([str(pointless)], input=stdin, text=True, stdout=fh, stderr=fh)

    mtz = job_dir / hklout
    if not mtz.exists():
        err = _read_log_error(log_path)
        return {"status": "error", "fatal": True,
                "message": f"Pointless failed: {err}"}

    return {"status": "completed", "message": f"completed"}


def _run_aimless(step, ctx, rp, pipeline_subdir):
    step_id = step.id
    rp._aimless(step_id, rp.params,
                prev_pointless_id=ctx["last_by_program"].get("pointless"),
                prev_aimless_id=ctx["last_by_program"].get("aimless"),
                apply_res_cutoff=step.aimless.apply_res_cutoff)

    mtz = pipeline_subdir / step_id / f"XDS_ASCII_{step_id}.mtz"
    if not mtz.exists():
        err = _read_log_error(pipeline_subdir / step_id / f"{step_id}.log")
        return {"status": "error", "fatal": True,
                "message": f"Aimless failed: {err}"}

    return {"status": "completed", "message": f"completed"}


def _run_rfree(step, ctx, rp, pipeline_subdir):
    rp._rfree(step.id, rp.params,
              prev_aimless_id=ctx["last_by_program"].get("aimless"))
    return {"status": "completed", "message": f"{step.id}: completed"}


def _run_xtriage(step, ctx, rp, pipeline_subdir):
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
    return handler(step, ctx, rp, pipeline_subdir)
