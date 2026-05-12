"""CCP4 / Phenix crystallographic refinement backend for the refine sub-pipeline.

All four programs that make up the qRefine scheme are implemented here as
individual step types so that users can reorder, repeat, or omit them via
PHIL.  Each step reads program-specific parameters from its own PHIL sub-scope
and dataset-specific values (MTZ path, starting PDB, suffix) from the params
dict that is loaded from params_{suffix}.json on the compute node.

Context dict keys set and consumed by these handlers
-----------------------------------------------------
  rfree_after_rigid_body  : float — set by rigid_body
  jelly_input             : str   — set by rigid_body (initial), updated by phaser
  mr_cutoff               : float — set by phaser (for downstream skip decisions)
  phaser_succeeded        : bool  — set by phaser (non-monomer)
  jelly_succeeded         : bool  — set by refmac (jelly, non-monomer)
  final_rfree             : float — set by refmac (restrained, non-monomer)
  phaser_monomer_succeeded: bool  — set by phaser (monomer)
  jelly_mono_input        : str   — set by phaser (monomer)
  jelly_mono_succeeded    : bool  — set by refmac (jelly, monomer)
"""

import pathlib
import shutil
import subprocess

from avicenna.utils.utils import format_log_line

SCOPE_NAME = "qrefine"

PROGRAMS = {
    "phenix.refine": """\
rigid_body
  .short_caption = Rigid-body refinement
  .help = Phenix rigid-body refinement of the input model (5 cycles).
{
}
""",
    "phenix.phaser": """\
phaser
  .short_caption = Phaser MR
  .help = Phenix Phaser molecular replacement.  Set monomer to True to mark this step as the optional monomer-model retry.
{
  mr_cutoff = 0.45
    .type = float
    .short_caption = Rfree cutoff
    .help = Rfree threshold above which MR is attempted.  Step is skipped when rigid-body Rfree is at or below this value.
    .expert_level = 0
  mr_copies = 1
    .type = int
    .short_caption = Search copies
    .help = Number of copies to search for in the primary MR run.
    .expert_level = 0
  mr_mw = 0.0
    .type = float
    .short_caption = Molecular weight (Da)
    .help = Molecular weight of the primary search model in Daltons.
    .expert_level = 0
  mr_seq_id = 90.0
    .type = float
    .short_caption = Sequence identity (%)
    .help = Sequence identity between primary search model and target (percent).
    .expert_level = 0
  mr_tncs = False
    .type = bool
    .short_caption = Use tNCS
    .help = Apply translational NCS correction in Phaser.
    .expert_level = 1
  monomer = False
    .type = bool
    .short_caption = Monomer retry
    .help = If True this step is the optional monomer-model retry rather than the primary MR attempt.
    .expert_level = 1
  mr_try_monomer = False
    .type = bool
    .short_caption = Enable monomer retry
    .help = When monomer=True, the retry only runs if mr_try_monomer=True and conditions are met.
    .expert_level = 0
  monomer_rfree_cutoff = 0.4
    .type = float
    .short_caption = Rfree cutoff for monomer retry
    .help = Monomer retry is attempted when final Rfree exceeds this value or primary MR failed.
    .expert_level = 1
  mr_copies_monomer = 1
    .type = int
    .short_caption = Monomer search copies
    .expert_level = 1
  mr_mw_monomer = 0.0
    .type = float
    .short_caption = Monomer molecular weight (Da)
    .expert_level = 1
}
""",
    "refmac5": """\
refmac
  .short_caption = REFMAC5 refinement
  .help = CCP4 REFMAC5 restrained or jelly-body refinement.  Set jelly to True for jelly-body mode and monomer to True to operate on the monomer MR solution.
{
  ncycles = 8
    .type = int
    .short_caption = Refinement cycles
    .help = Number of REFMAC5 cycles.
    .expert_level = 1
  jelly = False
    .type = bool
    .short_caption = Jelly-body mode
    .help = Use ridge-distance jelly-body restraints (sigma 0.01, 4 cycles typical) instead of standard restrained refinement.
    .expert_level = 0
  monomer = False
    .type = bool
    .short_caption = Monomer step
    .help = If True, operates on the monomer MR solution rather than the primary solution.
    .expert_level = 1
}
""",
    "fill": """\
fill
  .short_caption = Reflection filling
  .help = Fill missing reflections for panDDA input via uniqueify + CAD + phenix.maps.
{
  enabled = True
    .type = bool
    .short_caption = Enable
    .help = Whether to run the reflection-filling step.
    .expert_level = 0
}
""",
}


# ---------------------------------------------------------------------------
# Helpers (module-level so handlers can call them without a class)
# ---------------------------------------------------------------------------

def _log(tag, message, params, job_dir):
    job_id = params.get("job_id", params.get("pin", "?"))
    line = format_log_line(tag, message, context=job_id,
                           context_width=params.get("context_width"))
    with open(job_dir / "pipeline.log", "a") as fh:
        fh.write(line)
    print(line, end="", flush=True)


def _run_cmd(cmd, job_dir, stdin=None, log=None):
    result = subprocess.run(
        cmd, input=stdin, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=job_dir,
    )
    if log:
        (job_dir / log).write_text(result.stdout)
    return result.returncode


def _rfree_from_phenix_log(log_path):
    """Parse final Rfree from a phenix.refine log (last 'Final R-work =' line)."""
    try:
        for line in reversed(log_path.read_text().splitlines()):
            if "Final R-work =" in line:
                parts = line.split()
                return float(parts[parts.index("R-work") + 2])
    except (OSError, ValueError, IndexError):
        pass
    return 1.0


def _final_rfree_from_refmac_log(log_path):
    """Parse final Rfree from a REFMAC5 log ('Final results' block)."""
    try:
        lines = log_path.read_text().splitlines()
        for i, line in enumerate(lines):
            if "Final results" in line:
                for tl in lines[i + 1: i + 4]:
                    parts = tl.split()
                    if len(parts) >= 5:
                        return float(parts[4])
    except (OSError, ValueError, IndexError):
        pass
    return 1.0


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------

def _run_rigid_body(step, ctx, params, suffix, job_dir):
    mr_pdb = params["mr_pdb"]
    mtz    = params["mtz"]

    if suffix == "hoh":
        shutil.copy2(mr_pdb, job_dir / f"01-rigid-body_input_hoh.pdb")
    else:
        with open(mr_pdb) as fh:
            lines = [l for l in fh if not l.startswith("HETATM") or "HOH" not in l]
        (job_dir / f"01-rigid-body_input_nohoh.pdb").write_text("".join(lines))

    _run_cmd([
        "phenix.refine", str(job_dir / f"01-rigid-body_input_{suffix}.pdb"), mtz,
        "main.number_of_macro_cycles=5",
        "strategy=rigid_body",
        "write_eff_file=false", "write_geo_file=false", "write_def_file=false",
        "hydrogens.refine=riding",
        "miller_array.labels.name=F,SIGF",
        f"output.prefix=01-rigid-body_{suffix}",
    ], job_dir)

    log_path = job_dir / f"01-rigid-body_{suffix}_001.log"
    rfree = _rfree_from_phenix_log(log_path)

    return {
        "status": "completed",
        "message": f"01-rigid-body: rfree={rfree:.4f}",
        "ctx_updates": {
            "rfree_after_rigid_body": rfree,
            "jelly_input": str(job_dir / f"01-rigid-body_{suffix}_001.pdb"),
        },
    }


def _run_phaser(step, ctx, params, suffix, job_dir):
    s       = step.phaser
    mtz     = params["mtz"]
    monomer = s.monomer

    if monomer:
        if not s.mr_try_monomer:
            return {"status": "skipped", "message": "phaser (monomer): mr_try_monomer=False",
                    "ctx_updates": {"phaser_monomer_succeeded": False}}
        phaser_ok   = ctx.get("phaser_succeeded", False)
        final_rfree = ctx.get("final_rfree", 1.0)
        if phaser_ok and final_rfree <= s.monomer_rfree_cutoff:
            return {
                "status": "skipped",
                "message": (f"phaser (monomer): primary ok and "
                            f"rfree={final_rfree:.4f} ≤ {s.monomer_rfree_cutoff}"),
                "ctx_updates": {"phaser_monomer_succeeded": False},
            }
        mr_pdb_input = params.get("mr_pdb_monomer") or params.get("mr_pdb")
        copies  = s.mr_copies_monomer
        mw      = s.mr_mw_monomer
        prefix  = f"05-phaser_{suffix}"
        out_pdb = job_dir / f"05-phaser_{suffix}.1.pdb"
    else:
        rfree     = ctx.get("rfree_after_rigid_body", 1.0)
        mr_cutoff = s.mr_cutoff
        if rfree <= mr_cutoff:
            return {
                "status": "skipped",
                "message": f"phaser: rfree={rfree:.4f} ≤ {mr_cutoff} — MR not needed",
                "ctx_updates": {
                    "phaser_succeeded": False,
                    "mr_cutoff": mr_cutoff,
                },
            }
        copies  = s.mr_copies
        mw      = s.mr_mw
        prefix  = f"02-phaser_{suffix}"
        out_pdb = job_dir / f"02-phaser_{suffix}.1.pdb"

        if suffix == "hoh":
            src = pathlib.Path(params["mr_pdb"]).read_text().replace("HOH", "WWW")
            (job_dir / f"02-phaser_input_hoh.pdb").write_text(src)
            mr_pdb_input = str(job_dir / f"02-phaser_input_hoh.pdb")
        else:
            with open(params["mr_pdb"]) as fh:
                lines = [l for l in fh if not l.startswith("HETATM") or "HOH" not in l]
            (job_dir / f"02-phaser_input_nohoh.pdb").write_text("".join(lines))
            mr_pdb_input = str(job_dir / f"02-phaser_input_nohoh.pdb")

    _run_cmd([
        "phenix.phaser", mr_pdb_input, mtz,
        f"phaser.keywords.tncs.use={s.mr_tncs}",
        f"phaser.search_copies={copies}",
        f"phaser.mol_weight={mw}",
        f"phaser.model_identity={s.mr_seq_id}",
        f"phaser.keywords.general.root={prefix}",
    ], job_dir)

    succeeded = out_pdb.is_file()
    if succeeded and not monomer and suffix == "hoh":
        text = out_pdb.read_text().replace("WWW", "HOH").replace("  0.00 ", "  1.00 ")
        out_pdb.write_text(text)

    if monomer:
        return {
            "status": "completed",
            "message": f"phaser (monomer): {'success' if succeeded else 'failed'}",
            "ctx_updates": {
                "phaser_monomer_succeeded": succeeded,
                "jelly_mono_input": str(out_pdb) if succeeded else None,
            },
        }
    else:
        jelly_input = str(out_pdb) if succeeded else ctx.get("jelly_input")
        return {
            "status": "completed",
            "message": f"phaser: {'success' if succeeded else 'failed'}",
            "ctx_updates": {
                "phaser_succeeded": succeeded,
                "jelly_input": jelly_input,
                "mr_cutoff": s.mr_cutoff,
            },
        }


def _run_refmac(step, ctx, params, suffix, job_dir):
    s       = step.refmac
    mtz     = params["mtz"]
    jelly   = s.jelly
    monomer = s.monomer
    ncycles = s.ncycles

    if monomer:
        if not ctx.get("phaser_monomer_succeeded", False):
            return {"status": "skipped",
                    "message": f"refmac (monomer, jelly={jelly}): phaser-monomer failed or skipped"}
        if jelly:
            xyz_in  = ctx.get("jelly_mono_input", "")
            xyz_out = job_dir / f"06-jelly_{suffix}.pdb"
            hkl_out = job_dir / f"06-jelly_{suffix}.mtz"
            log_out = f"06-jelly_{suffix}.log"
            label   = "06-jelly (monomer)"
        else:
            if not ctx.get("jelly_mono_succeeded", False):
                return {"status": "skipped",
                        "message": "refmac (monomer, restrained): monomer jelly failed or skipped"}
            xyz_in  = str(job_dir / f"06-jelly_{suffix}.pdb")
            xyz_out = job_dir / f"final_monomer_{suffix}.pdb"
            hkl_out = job_dir / f"final_monomer_{suffix}.mtz"
            log_out = f"07-refmac_{suffix}.log"
            label   = "07-refmac (monomer)"
    else:
        phaser_ok = ctx.get("phaser_succeeded", False)
        rfree     = ctx.get("rfree_after_rigid_body", 1.0)
        mr_cutoff = ctx.get("mr_cutoff", 0.45)
        if not phaser_ok and rfree >= mr_cutoff:
            flag = "jelly_succeeded" if jelly else None
            updates = {flag: False} if flag else {}
            return {"status": "skipped",
                    "message": f"refmac (jelly={jelly}): phaser failed and rfree too high",
                    "ctx_updates": updates}
        if jelly:
            xyz_in  = ctx.get("jelly_input", "")
            xyz_out = job_dir / f"03-jelly_{suffix}.pdb"
            hkl_out = job_dir / f"03-jelly_{suffix}.mtz"
            log_out = f"03-jelly_{suffix}.log"
            label   = "03-jelly"
        else:
            if not ctx.get("jelly_succeeded", False):
                return {"status": "skipped",
                        "message": "refmac (restrained): jelly failed or skipped"}
            xyz_in  = str(job_dir / f"03-jelly_{suffix}.pdb")
            xyz_out = job_dir / f"final_{suffix}.pdb"
            hkl_out = job_dir / f"final_{suffix}.mtz"
            log_out = f"04-refmac_{suffix}.log"
            label   = "04-refmac"

    stdin_kw = (
        "make newligand continue\n"
        "refinement type restrained\n"
        "weight auto\n"
        + ("ridge distance sigma 0.01\nmake hydrogen no\n" if jelly else "")
        + f"ncycle {ncycles}\n"
        "END\n"
    )

    _run_cmd(
        ["refmac5",
         "labin", "FP=F", "SIGFP=SIGF", "FREE=FreeR_flag",
         "XYZIN", xyz_in,
         "HKLIN", mtz,
         "XYZOUT", str(xyz_out),
         "HKLOUT", str(hkl_out)],
        job_dir, stdin=stdin_kw, log=log_out,
    )

    ctx_updates = {}
    if jelly and monomer:
        ctx_updates["jelly_mono_succeeded"] = xyz_out.is_file()
    elif jelly and not monomer:
        ctx_updates["jelly_succeeded"] = xyz_out.is_file()
    elif not jelly and not monomer:
        log_path = job_dir / log_out
        ctx_updates["final_rfree"] = _final_rfree_from_refmac_log(log_path)

    return {
        "status": "completed",
        "message": f"{label}: completed",
        "ctx_updates": ctx_updates,
    }


def _run_fill(step, ctx, params, suffix, job_dir):
    if not step.fill.enabled:
        return {"status": "skipped", "message": "fill: disabled"}

    for pdb_prefix in (f"final_monomer_{suffix}", f"final_{suffix}"):
        pdb_path = job_dir / f"{pdb_prefix}.pdb"
        mtz_path = job_dir / f"{pdb_prefix}.mtz"
        if pdb_path.is_file() and mtz_path.is_file():
            break
    else:
        return {"status": "error", "fatal": False,
                "message": "fill: no final PDB/MTZ pair found"}

    _run_cmd(
        ["uniqueify", "-f", "FreeR_flag", str(mtz_path), "fill_01_unique.mtz"],
        job_dir, log="fill_01_unique.log",
    )
    if not (job_dir / "fill_01_unique.mtz").is_file():
        return {"status": "error", "fatal": False, "message": "fill: uniqueify failed"}

    _run_cmd(
        ["cad", "hklin1", "fill_01_unique.mtz", "hklout", "fill_02_cad.mtz"],
        job_dir,
        stdin="monitor BRIEF\nlabin file 1 ALL\nresolution file 1 999.0 HIGHRES\nEND\n",
        log="fill_02_cad.log",
    )
    if not (job_dir / "fill_02_cad.mtz").is_file():
        return {"status": "error", "fatal": False, "message": "fill: CAD failed"}

    _run_cmd(
        ["phenix.maps", str(pdb_path), "fill_02_cad.mtz"],
        job_dir, log="fill_03_maps.log",
    )
    dest = job_dir / f"final_{suffix}_filled.mtz"
    for candidate in ("fill_02_cad_map_coeffs.mtz", "fill_02_cad_map_coefficients.mtz"):
        src = job_dir / candidate
        if src.is_file():
            src.rename(dest)
            return {"status": "completed", "message": f"fill: {dest.name} written"}

    return {"status": "error", "fatal": False,
            "message": "fill: phenix.maps output not found"}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_HANDLERS = {
    "phenix.refine": _run_rigid_body,
    "phenix.phaser": _run_phaser,
    "refmac5":       _run_refmac,
    "fill":          _run_fill,
}


def run_step(step, ctx, params, suffix, job_dir):
    handler = _HANDLERS.get(step.program)
    if handler is None:
        return {"status": "error", "fatal": True,
                "message": f"no handler registered for program '{step.program}'"}
    return handler(step, ctx, params, suffix, job_dir)
