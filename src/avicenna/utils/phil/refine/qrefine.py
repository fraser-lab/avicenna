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
  rfree_threshold         : float — set by phaser (for downstream skip decisions)
  phaser_succeeded        : bool  — set by phaser
  jelly_succeeded         : bool  — set by refmac (jelly)
  final_rfree             : float — set by refmac (restrained)
"""

import pathlib
import shutil
import subprocess

from avicenna.utils.utils import format_log_line

SCOPE_NAME = "qrefine"

PROGRAMS = {
    "phenix.refine": """\
phenix
  .short_caption = Phenix refinement
  .help = phenix.refine settings.  Parameters left as None fall back to phenix.refine's own defaults.  Use extra_args for any phenix parameter not exposed here.
{
  strategy = None
    .type = str
    .short_caption = Refinement strategy
    .help = phenix.refine target(s), e.g. rigid_body or individual_sites+individual_adp+occupancies.  None uses the phenix default.
    .expert_level = 0
  number_of_macro_cycles = None
    .type = int
    .short_caption = Macro-cycles
    .help = Number of refinement macro-cycles (main.number_of_macro_cycles).  None uses the phenix default.
    .expert_level = 0
  hydrogens = individual riding
    .type = choice
    .short_caption = Hydrogen treatment
    .help = How to treat hydrogens (hydrogens.refine).  None (neither selected) uses the phenix default (Auto).
    .expert_level = 0
  extra_args = None
    .type = strings
    .short_caption = Extra phenix.refine arguments
    .help = Verbatim phenix.refine command-line overrides forwarded as-is, e.g. "nqh_flips=False" "ordered_solvent=True".  phenix.refine validates these.
    .expert_level = 0
}
""",
    "phenix.phaser": """\
phaser
  .short_caption = Phaser MR
  .help = Phenix Phaser molecular replacement.  Runs only when rigid-body refinement leaves Rfree above rfree_threshold.
{
  rfree_threshold = 0.45
    .type = float
    .short_caption = Rfree threshold
    .help = MR is attempted only when the rigid-body Rfree exceeds this value.
    .expert_level = 0
  tncs = False
    .type = bool
    .short_caption = Use tNCS
    .help = Apply translational NCS correction in Phaser.
    .expert_level = 1
  alt_search_model = None
    .type = path
    .short_caption = Alternate search model
    .help = Optional search model used in place of input.reference_pdb -- e.g. a monomer, or a perturbed / poly-Ala-truncated model.  When None the reference is used.
    .expert_level = 0
  asu_composition
    .short_caption = ASU composition
    .help = Asymmetric-unit contents Phaser uses to estimate solvent content.
  {
    derive_from_pdb = True
      .type = bool
      .short_caption = Derive from model
      .help = Estimate molecular weight and copy number from the search model and reference_pdb instead of the values below.  Assumes reference_pdb models the full ASU and the crystals are isomorphous to it.
      .expert_level = 0
    num_copies = 1
      .type = int
      .short_caption = Copies in ASU
      .help = Number of copies of the search model to find.  Ignored when derive_from_pdb=True.
      .expert_level = 0
    mw = 0.0
      .type = float
      .short_caption = Molecular weight (Da)
      .help = Molecular weight of the search model in Daltons.  Ignored when derive_from_pdb=True.
      .expert_level = 0
    seq_id = 90.0
      .type = float
      .short_caption = Sequence identity (%)
      .help = Sequence identity between search model and target (percent).
      .expert_level = 0
  }
  extra_args = None
    .type = strings
    .short_caption = Extra phenix.phaser arguments
    .help = Verbatim phenix.phaser command-line overrides forwarded as-is (e.g. a sequence file or explicit composition keywords).  phenix.phaser validates these.
    .expert_level = 0
}
""",
    "refmac5": """\
refmac
  .short_caption = REFMAC5 refinement
  .help = CCP4 REFMAC5 restrained or jelly-body refinement.  Set jelly to True for jelly-body mode.
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
# ASU composition derivation from the reference PDB (quick-and-dirty)
# ---------------------------------------------------------------------------

# Average masses of free amino acids (Da).  MSE (selenomethionine) is treated
# as MET.  Non-standard residues / ligands / waters are ignored.
_AA_MASS = {
    "ALA": 89.09, "ARG": 174.20, "ASN": 132.12, "ASP": 133.10, "CYS": 121.16,
    "GLN": 146.15, "GLU": 147.13, "GLY": 75.07, "HIS": 155.16, "ILE": 131.17,
    "LEU": 131.17, "LYS": 146.19, "MET": 149.21, "PHE": 165.19, "PRO": 115.13,
    "SER": 105.09, "THR": 119.12, "TRP": 204.23, "TYR": 181.19, "VAL": 117.15,
    "MSE": 149.21,
}
_WATER_MASS = 18.015
_AVG_RES_MASS = 110.0   # used to fill unmodeled gaps (by residue-number span)


def _protein_mw_and_chains(pdb_path, gap_fill=True):
    """Return (molecular_weight_Da, n_protein_chains) from a PDB's ATOM records.

    Counts standard amino-acid residues (unique per chain+resseq), sums average
    masses minus one water per intra-chain peptide bond.  With gap_fill, missing
    residues (breaks in the resseq span) are charged the average residue mass so
    a partial model does not under-estimate the protein MW.  Returns (None, 0)
    if nothing parseable is found.
    """
    if not pdb_path:
        return None, 0
    chains = {}   # chain_id -> {resseq: resname}
    try:
        with open(pdb_path) as fh:
            for line in fh:
                if not line.startswith(("ATOM", "HETATM")):
                    continue
                resname = line[17:20].strip()
                if resname not in _AA_MASS:
                    continue
                try:
                    resseq = int(line[22:26])
                except ValueError:
                    continue
                chains.setdefault(line[21], {})[resseq] = resname
    except OSError:
        return None, 0
    if not chains:
        return None, 0
    total = 0.0
    for residues in chains.values():
        modeled = sum(_AA_MASS[r] for r in residues.values())
        n_res = len(residues)
        if gap_fill:
            span = max(residues) - min(residues) + 1
            modeled += max(0, span - n_res) * _AVG_RES_MASS
            n_res = span
        total += modeled - (n_res - 1) * _WATER_MASS   # peptide bonds
    return total, len(chains)


def _derive_composition(reference_pdb, search_model):
    """Return (mol_weight, num_copies) for Phaser from the model files.

    mol_weight = MW of the search model; num_copies = (protein chains in the
    reference) / (chains in the search model), so MW and copies stay consistent
    whether the search model is the full ASU (1 copy) or a monomer (N copies).
    Returns (None, None) when the search model cannot be parsed.
    """
    model_mw, model_chains = _protein_mw_and_chains(search_model)
    if model_mw is None or model_chains == 0:
        return None, None
    _, ref_chains = _protein_mw_and_chains(reference_pdb)
    copies = max(1, round(ref_chains / model_chains)) if ref_chains else 1
    return model_mw, copies


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------

def _run_phenix(step, ctx, params, suffix, job_dir):
    s      = step.phenix
    mr_pdb = params["mr_pdb"]
    mtz    = params["mtz"]

    if suffix == "hoh":
        shutil.copy2(mr_pdb, job_dir / f"01-rigid-body_input_hoh.pdb")
    else:
        with open(mr_pdb) as fh:
            lines = [l for l in fh if not l.startswith("HETATM") or "HOH" not in l]
        (job_dir / f"01-rigid-body_input_nohoh.pdb").write_text("".join(lines))

    cmd = ["phenix.refine",
           str(job_dir / f"01-rigid-body_input_{suffix}.pdb"), mtz]
    # Typed params: emit only when set; None falls back to the phenix default.
    if s.strategy is not None:
        cmd.append(f"strategy={s.strategy}")
    if s.number_of_macro_cycles is not None:
        cmd.append(f"main.number_of_macro_cycles={s.number_of_macro_cycles}")
    if s.hydrogens is not None:
        cmd.append(f"hydrogens.refine={s.hydrogens}")
    # Housekeeping + I/O plumbing set by the pipeline (not user-facing).
    cmd += [
        "write_eff_file=false", "write_geo_file=false", "write_def_file=false",
        "miller_array.labels.name=F,SIGF",
        f"output.prefix=01-rigid-body_{suffix}",
    ]
    # Verbatim pass-through: forwarded as-is, validated by phenix.refine itself.
    cmd += list(s.extra_args or [])

    _run_cmd(cmd, job_dir)

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
    s         = step.phaser
    mtz       = params["mtz"]
    reference = params["mr_pdb"]

    # MR is only needed when rigid-body refinement didn't get close enough.
    rfree = ctx.get("rfree_after_rigid_body", 1.0)
    if rfree <= s.rfree_threshold:
        return {"status": "skipped",
                "message": f"phaser: rfree={rfree:.4f} ≤ {s.rfree_threshold} — MR not needed",
                "ctx_updates": {"phaser_succeeded": False,
                                "rfree_threshold": s.rfree_threshold}}

    # Search model: the alternate model if given, else the reference.
    search_model = s.alt_search_model or reference
    comp = s.asu_composition
    if comp.derive_from_pdb:
        mw, copies = _derive_composition(reference, search_model)
        if mw is None:            # parse failed — fall back to explicit values
            mw, copies = comp.mw, comp.num_copies
    else:
        mw, copies = comp.mw, comp.num_copies

    prefix  = f"02-phaser_{suffix}"
    out_pdb = job_dir / f"02-phaser_{suffix}.1.pdb"
    if suffix == "hoh":
        src = pathlib.Path(search_model).read_text().replace("HOH", "WWW")
        (job_dir / f"02-phaser_input_hoh.pdb").write_text(src)
        model_in = str(job_dir / f"02-phaser_input_hoh.pdb")
    else:
        with open(search_model) as fh:
            lines = [l for l in fh if not l.startswith("HETATM") or "HOH" not in l]
        (job_dir / f"02-phaser_input_nohoh.pdb").write_text("".join(lines))
        model_in = str(job_dir / f"02-phaser_input_nohoh.pdb")

    cmd = [
        "phenix.phaser", model_in, mtz,
        f"phaser.keywords.tncs.use={s.tncs}",
        f"phaser.search_copies={copies}",
        f"phaser.model_identity={comp.seq_id}",
        f"phaser.keywords.general.root={prefix}",
    ]
    if mw:   # omit mol_weight when 0/None so Phaser estimates it itself
        cmd.append(f"phaser.mol_weight={mw}")
    cmd += list(s.extra_args or [])
    _run_cmd(cmd, job_dir)

    succeeded = out_pdb.is_file()
    if succeeded and suffix == "hoh":
        text = out_pdb.read_text().replace("WWW", "HOH").replace("  0.00 ", "  1.00 ")
        out_pdb.write_text(text)

    jelly_input = str(out_pdb) if succeeded else ctx.get("jelly_input")
    return {
        "status": "completed",
        "message": f"phaser: {'success' if succeeded else 'failed'}",
        "ctx_updates": {
            "phaser_succeeded": succeeded,
            "jelly_input": jelly_input,
            "rfree_threshold": s.rfree_threshold,
        },
    }


def _run_refmac(step, ctx, params, suffix, job_dir):
    s       = step.refmac
    mtz     = params["mtz"]
    jelly   = s.jelly
    ncycles = s.ncycles

    phaser_ok = ctx.get("phaser_succeeded", False)
    rfree     = ctx.get("rfree_after_rigid_body", 1.0)
    rfree_threshold = ctx.get("rfree_threshold", 0.45)
    if not phaser_ok and rfree >= rfree_threshold:
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
    if jelly:
        ctx_updates["jelly_succeeded"] = xyz_out.is_file()
    else:
        ctx_updates["final_rfree"] = _final_rfree_from_refmac_log(job_dir / log_out)

    return {
        "status": "completed",
        "message": f"{label}: completed",
        "ctx_updates": ctx_updates,
    }


def _run_fill(step, ctx, params, suffix, job_dir):
    if not step.fill.enabled:
        return {"status": "skipped", "message": "fill: disabled"}

    pdb_path = job_dir / f"final_{suffix}.pdb"
    mtz_path = job_dir / f"final_{suffix}.mtz"
    if not (pdb_path.is_file() and mtz_path.is_file()):
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
    "phenix.refine": _run_phenix,
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
