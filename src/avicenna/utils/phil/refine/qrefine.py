"""CCP4 / Phenix crystallographic refinement backend for the refine sub-pipeline.

All four programs that make up the qRefine scheme are implemented here as
individual step types so that users can reorder, repeat, or omit them via
PHIL.  Each step reads program-specific parameters from its own PHIL sub-scope
and dataset-specific values (MTZ path, starting PDB, suffix) from the params
dict that is loaded from params_{suffix}.json on the compute node.

Context dict keys set and consumed by these handlers
-----------------------------------------------------
  rfree_after_rigid_body  : float -- set by a phenix.refine (rigid_body) step, if any
  jelly_input             : str   -- set by phaser (default) or a rigid_body step, used by refmac jelly
  data_mtz                : str   -- set by phaser; the data MTZ (in the solution frame) refmac refines against
  rfree_threshold         : float -- set by phaser (for downstream skip decisions)
  phaser_succeeded        : bool  -- set by phaser
  jelly_succeeded         : bool  -- set by refmac (jelly)
  final_rfree             : float -- set by refmac (restrained)
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
  .help = Phenix Phaser molecular replacement.  The default first step -- Phaser also resolves the enantiomorphic space group (e.g. P41212 vs P43212), which a bare rigid-body step cannot.  If a refinement step (e.g. phenix.refine rigid body) precedes it and already reaches Rfree <= rfree_threshold, Phaser is skipped.
{
  rfree_threshold = 0.45
    .type = float
    .short_caption = Rfree threshold
    .help = When a preceding refinement step has set an Rfree, MR is attempted only if that Rfree exceeds this value.  With no preceding step (the default), MR always runs.
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
    """Run *cmd* in *job_dir*, streaming combined stdout+stderr to a log file as
    it is produced; return the process return code.

    Streaming straight to disk -- rather than buffering in memory and writing the
    log once the program exits -- keeps the log file's mtime advancing throughout
    a long run, which is the progress signal a stall watchdog relies on to tell a
    legitimately-long job from a hung one.  It also leaves a partial log behind if
    the job is killed mid-run, which is invaluable for debugging.  Most
    crystallography programs write their logs this way already.

    *log* names the file; when omitted it defaults to ``<program>.log`` (stdout
    that was previously discarded is now kept).
    """
    log_path = job_dir / (log or f"{pathlib.Path(cmd[0]).name}.log")
    with open(log_path, "w") as fh:
        result = subprocess.run(
            cmd, input=stdin, text=True,
            stdout=fh, stderr=subprocess.STDOUT,
            cwd=job_dir,
        )
    return result.returncode


def _pdb_stats(pdb_path):
    """Return ``(rwork, rfree, res_high)`` from a refined PDB's REMARK 3 records.

    Reads the standard PDB REMARK 3 fields written by BOTH phenix.refine and
    REFMAC5 -- ``R VALUE            (WORKING SET) :``, ``FREE R VALUE
    :`` (the padded standalone record, distinct from the ``FREE R VALUE TEST
    SET ...`` / ``BIN FREE R VALUE ...`` lines), and ``RESOLUTION RANGE HIGH
    (ANGSTROMS) :``.  Reading the model a step actually produced is the most
    robust signal: it is the canonical refined statistic and matches how final
    results are scraped elsewhere (see utils.ScrapeRefine._parse_pdb_stats).
    Any field that is absent or unparseable comes back None so the caller can
    format only what is available (e.g. the model was never written)."""
    rwork = rfree = res = None
    try:
        for line in pdb_path.read_text().splitlines():
            if not line.startswith("REMARK   3"):
                continue
            if "R VALUE            (WORKING SET) :" in line:
                try:
                    rwork = float(line.split(":")[-1])
                except ValueError:
                    pass
            elif "FREE R VALUE                     :" in line:
                try:
                    rfree = float(line.split(":")[-1])
                except ValueError:
                    pass
            elif "RESOLUTION RANGE HIGH (ANGSTROMS) :" in line:
                try:
                    res = float(line.split(":")[-1])
                except ValueError:
                    pass
    except OSError:
        pass
    return rwork, rfree, res


def _rwork_rfree_from_phenix_log(log_path):
    """Parse final ``(rwork, rfree)`` from a phenix.refine log.

    Fallback for when the output PDB is unavailable.  The summary line is
    ``Final R-work = 0.2305, R-free = 0.2451``; commas are stripped before
    tokenising (the value token is otherwise ``'0.2305,'`` and float() chokes).
    Returns ``(None, None)`` when no parseable summary line is present."""
    try:
        for line in reversed(log_path.read_text().splitlines()):
            if "Final R-work =" in line:
                parts = line.replace(",", " ").split()
                return (float(parts[parts.index("R-work") + 2]),
                        float(parts[parts.index("R-free") + 2]))
    except (OSError, ValueError, IndexError):
        pass
    return None, None


def _rwork_rfree_from_refmac_log(log_path):
    """Parse final ``(rwork, rfree)`` from a REFMAC5 'Final results' block.

    The block lists Initial and Final columns; we take the Final (last) value::

        $TEXT:Result: $$ Final results $$
                              Initial    Final
                   R factor    0.2207   0.2159
                     R free    0.2287   0.2312

    NB: the ``R free`` row carries only four whitespace-separated tokens, so the
    old positional ``parts[4]`` read never matched -- this keys off the field
    name instead and reads the final column.  Returns ``(None, None)`` when the
    block is absent."""
    rwork = rfree = None
    try:
        lines = log_path.read_text().splitlines()
        for i, line in enumerate(lines):
            if "Final results" not in line:
                continue
            for tl in lines[i + 1: i + 8]:
                parts = tl.split()
                if len(parts) < 4 or parts[0] != "R":
                    continue
                try:
                    val = float(parts[-1])
                except ValueError:
                    continue
                if parts[1] == "factor":
                    rwork = val
                elif parts[1] == "free":
                    rfree = val
            break
    except OSError:
        pass
    return rwork, rfree


def _llg_tfz_from_phaser_log(log_path):
    """Parse refined ``(llg, tfz)`` from a Phaser log's accepted-solution record.

    The accepted solution is summarised on a ``SOLU SET`` line that carries the
    start figures followed by the refined ones, e.g.::

        SOLU SET  RF*0 TF*0 LLG=25115 TFZ==173.0 PAK=1 LLG=25117 TFZ==173.0

    Tokens are scanned left-to-right so the LAST ``LLG=`` / ``TFZ(=)`` win and
    the refined figures are reported (``TFZ==`` splits to the value on the final
    ``=``).  Returns ``(None, None)`` when no such line is found (MR failed)."""
    llg = tfz = None
    try:
        for line in reversed(log_path.read_text().splitlines()):
            if "SOLU SET" in line and "LLG=" in line:
                for tok in line.split():
                    if tok.startswith("LLG="):
                        try:
                            llg = float(tok.split("=")[-1])
                        except ValueError:
                            pass
                    elif tok.startswith("TFZ="):
                        try:
                            tfz = float(tok.split("=")[-1])
                        except ValueError:
                            pass
                break
    except OSError:
        pass
    return llg, tfz


def _fmt_r_stats(rwork, rfree, res=None):
    """Format the available R-stats into a compact log detail.

    Order is resolution, Rwork, Rfree (the completed-job line passes all three;
    per-step refinement lines omit resolution).  Returns "" when nothing is
    available, so callers can fall back to a plain status word."""
    parts = []
    if res is not None:
        parts.append(f"res={res:.2f}A")
    if rwork is not None:
        parts.append(f"Rwork={rwork:.4f}")
    if rfree is not None:
        parts.append(f"Rfree={rfree:.4f}")
    return " ".join(parts)


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
        # Select BOTH the data array (F,SIGF) and the R-free flag array
        # (FreeR_flag) explicitly.  These XDS-derived MTZs also carry an integer
        # ISYM column that phenix.refine sometimes deems an equally-suitable
        # R-free array, aborting with "Multiple equally suitable arrays of R-free
        # flags found".  Naming the two arrays we want (a second miller_array
        # selection) excludes ISYM and makes the choice unambiguous for every
        # phenix.refine step, not just rigid body.
        "miller_array.labels.name=F,SIGF",
        "miller_array.labels.name=FreeR_flag",
        f"output.prefix=01-rigid-body_{suffix}",
    ]
    # Verbatim pass-through: forwarded as-is, validated by phenix.refine itself.
    cmd += list(s.extra_args or [])

    _run_cmd(cmd, job_dir, log=f"01-rigid-body_{suffix}_stdout.log")

    out_pdb = job_dir / f"01-rigid-body_{suffix}_001.pdb"
    rwork, rfree, _ = _pdb_stats(out_pdb)
    if rfree is None:   # no model written (e.g. aborted) -- try the log
        rwork, rfree = _rwork_rfree_from_phenix_log(
            job_dir / f"01-rigid-body_{suffix}_001.log")

    # rfree gates the downstream Phaser skip; default to 1.0 (always run MR) when
    # unknown so a parse miss never silently suppresses molecular replacement.
    rfree_ctx = rfree if rfree is not None else 1.0
    return {
        "status": "completed",
        "message": _fmt_r_stats(rwork, rfree) or "completed",
        "ctx_updates": {
            "rfree_after_rigid_body": rfree_ctx,
            "jelly_input": str(job_dir / f"01-rigid-body_{suffix}_001.pdb"),
        },
    }


# Enantiomorphic space-group pairs, by international number.  A Phaser hand flip
# between the two members of a pair is a pure relabel: the hkl set is unchanged,
# so R-free flags from the original MTZ correspond reflection-for-reflection and
# carry over when the space group is re-stamped.  (There are exactly 11 such
# pairs among the 3D space groups.)
_ENANTIOMORPH_PAIRS = frozenset(frozenset(p) for p in [
    (76, 78), (91, 95), (92, 96), (144, 145), (151, 153), (152, 154),
    (169, 173), (170, 172), (178, 179), (180, 181), (212, 213),
])


def _mtz_space_group_number(mtz_path):
    """Return the international space-group number stored in an MTZ, or None."""
    try:
        out = subprocess.run(["phenix.mtz.dump", str(mtz_path)],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "Space group number from file:" in line:
                return int(line.split(":")[1])
    except (OSError, ValueError):
        pass
    return None


def _refmac_data_mtz(phaser_mtz, orig_mtz, suffix, job_dir):
    """Return the data MTZ (F, SIGF, FreeR_flag) downstream refinement should use.

    Modularity: after MR, refinement consumes PHASER's result frame rather than
    reaching back to the original input.  Phaser may resolve a space-group
    ambiguity (commonly an enantiomorph flip, e.g. P41212 -> P43212); its output
    MTZ holds F/SIGF in the chosen frame but carries no R-free flags.  We restore
    them per the user's policy -- transfer from the original ctruncate MTZ when
    feasible, generate fresh otherwise:

    * Solution space group == input: the original ctruncate MTZ is already in the
      right frame and keeps its flags -- use it unchanged (the usual isomorphous
      case; no extra work).
    * Enantiomorphic flip: a pure relabel (hkl unchanged), so re-stamp the
      original's space group to the solution.  F/SIGF AND the original FreeR_flag
      (e.g. a test set copied from the reference) carry over intact.
    * Any other change: indices may not correspond, so use Phaser's (correctly
      transformed) F/SIGF and generate a fresh FreeR_flag with freerflag.
    """
    in_sg  = _mtz_space_group_number(orig_mtz)
    sol_sg = _mtz_space_group_number(phaser_mtz)
    if in_sg is not None and in_sg == sol_sg:
        return str(orig_mtz)

    out = job_dir / f"02-phaser_{suffix}_data.mtz"
    if (in_sg is not None and sol_sg is not None
            and frozenset((in_sg, sol_sg)) in _ENANTIOMORPH_PAIRS):
        # Pure relabel: re-stamp the space group, keeping all columns + flags.
        _run_cmd(["cad", "hklin1", str(orig_mtz), "hklout", str(out)],
                 job_dir, stdin=f"LABIN FILE 1 ALL\nSYMMETRY {sol_sg}\nEND\n",
                 log=f"02-phaser_{suffix}_data.log")
        if out.is_file():
            return str(out)

    # Transfer not feasible: Phaser's F/SIGF + a freshly generated R-free set.
    _run_cmd(["freerflag", "hklin", str(phaser_mtz), "hklout", str(out)],
             job_dir, stdin="UNIQUE\nEND\n", log=f"02-phaser_{suffix}_data.log")
    return str(out) if out.is_file() else str(orig_mtz)


def _run_phaser(step, ctx, params, suffix, job_dir):
    s         = step.phaser
    mtz       = params["mtz"]
    reference = params["mr_pdb"]

    # Skip MR only if a preceding refinement step already reached the threshold.
    # With no such step (the default), rfree defaults to 1.0 so MR always runs.
    rfree = ctx.get("rfree_after_rigid_body", 1.0)
    if rfree <= s.rfree_threshold:
        return {"status": "skipped",
                "message": f"rfree={rfree:.4f} ≤ {s.rfree_threshold} — MR not needed",
                "ctx_updates": {"phaser_succeeded": False,
                                "rfree_threshold": s.rfree_threshold}}

    # Search model: the alternate model if given, else the reference.
    search_model = s.alt_search_model or reference
    comp = s.asu_composition
    if comp.derive_from_pdb:
        mw, copies = _derive_composition(reference, search_model)
        if mw is None:            # parse failed -- fall back to explicit values
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
        # phaser.mol_weight is integer-typed; a derived (fractional) MW must be
        # rounded or phaser aborts ("Error interpreting ... as an integer").
        cmd.append(f"phaser.mol_weight={int(round(mw))}")
    cmd += list(s.extra_args or [])
    _run_cmd(cmd, job_dir, log=f"02-phaser_{suffix}_stdout.log")

    succeeded = out_pdb.is_file()
    if succeeded and suffix == "hoh":
        text = out_pdb.read_text().replace("WWW", "HOH").replace("  0.00 ", "  1.00 ")
        out_pdb.write_text(text)

    jelly_input = str(out_pdb) if succeeded else ctx.get("jelly_input")
    ctx_updates = {
        "phaser_succeeded": succeeded,
        "jelly_input": jelly_input,
        "rfree_threshold": s.rfree_threshold,
    }
    if succeeded:
        # Hand the corrected data forward: downstream refmac refines against
        # Phaser's solution frame (its space group / hand), not params["mtz"].
        ctx_updates["data_mtz"] = _refmac_data_mtz(
            job_dir / f"02-phaser_{suffix}.1.mtz", params["mtz"], suffix, job_dir)

    if succeeded:
        # phenix.phaser writes its log to <root>.log; fall back to the captured
        # stdout if that is missing.
        llg, tfz = _llg_tfz_from_phaser_log(job_dir / f"{prefix}.log")
        if llg is None and tfz is None:
            llg, tfz = _llg_tfz_from_phaser_log(job_dir / f"{prefix}_stdout.log")
        stats = []
        if llg is not None:
            stats.append(f"LLG={llg:.0f}")
        if tfz is not None:
            stats.append(f"TFZ={tfz:.1f}")
        message = "success" + (f" ({' '.join(stats)})" if stats else "")
    else:
        message = "failed"
    return {
        "status": "completed",
        "message": message,
        "ctx_updates": ctx_updates,
    }


def _run_refmac(step, ctx, params, suffix, job_dir):
    s       = step.refmac
    # Consume the upstream step's data frame (Phaser's solution, if it ran),
    # falling back to the original input MTZ when nothing corrected it.
    mtz     = ctx.get("data_mtz", params["mtz"])
    jelly   = s.jelly
    ncycles = s.ncycles

    phaser_ok = ctx.get("phaser_succeeded", False)
    rfree     = ctx.get("rfree_after_rigid_body", 1.0)
    rfree_threshold = ctx.get("rfree_threshold", 0.45)
    if not phaser_ok and rfree >= rfree_threshold:
        flag = "jelly_succeeded" if jelly else None
        updates = {flag: False} if flag else {}
        return {"status": "skipped",
                "message": f"phaser failed and rfree too high (jelly={jelly})",
                "ctx_updates": updates}
    if jelly:
        xyz_in  = ctx.get("jelly_input", "")
        xyz_out = job_dir / f"03-jelly_{suffix}.pdb"
        hkl_out = job_dir / f"03-jelly_{suffix}.mtz"
        log_out = f"03-jelly_{suffix}.log"
    else:
        if not ctx.get("jelly_succeeded", False):
            return {"status": "skipped",
                    "message": "jelly failed or skipped"}
        xyz_in  = str(job_dir / f"03-jelly_{suffix}.pdb")
        xyz_out = job_dir / f"final_{suffix}.pdb"
        hkl_out = job_dir / f"final_{suffix}.mtz"
        log_out = f"04-refmac_{suffix}.log"

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

    # Report R-stats from the model this step produced (REMARK 3, the canonical
    # refined statistic), falling back to the log's 'Final results' block.
    rwork, rfree, res = _pdb_stats(xyz_out)
    if rwork is None and rfree is None:
        rwork, rfree = _rwork_rfree_from_refmac_log(job_dir / log_out)

    ctx_updates = {}
    if jelly:
        ctx_updates["jelly_succeeded"] = xyz_out.is_file()
    else:
        if rfree is not None:
            ctx_updates["final_rfree"] = rfree
        # Hand the completed-job summary (resolution + R-stats) to the
        # interpreter's DONE line; this is the last refinement step.
        ctx_updates["final_summary"] = _fmt_r_stats(rwork, rfree, res)

    return {
        "status": "completed",
        "message": _fmt_r_stats(rwork, rfree) or "completed",
        "ctx_updates": ctx_updates,
    }


def _run_fill(step, ctx, params, suffix, job_dir):
    if not step.fill.enabled:
        return {"status": "skipped", "message": "disabled"}

    pdb_path = job_dir / f"final_{suffix}.pdb"
    mtz_path = job_dir / f"final_{suffix}.mtz"
    if not (pdb_path.is_file() and mtz_path.is_file()):
        return {"status": "error", "fatal": False,
                "message": "no final PDB/MTZ pair found"}

    _run_cmd(
        ["uniqueify", "-f", "FreeR_flag", str(mtz_path), "fill_01_unique.mtz"],
        job_dir, log="fill_01_unique.log",
    )
    if not (job_dir / "fill_01_unique.mtz").is_file():
        return {"status": "error", "fatal": False, "message": "uniqueify failed"}

    # NB: no "resolution file 1 ..." line here.  An earlier version carried a
    # literal "resolution file 1 999.0 HIGHRES" placeholder whose unsubstituted
    # HIGHRES token made CAD's resolution window invalid -- it rejected *every*
    # reflection (leaving a 1-reflection MTZ), so phenix.maps downstream aborted
    # with "No array of observed xray data found".  We want the full reflection
    # list passed through unchanged, so the resolution restriction is omitted.
    _run_cmd(
        ["cad", "hklin1", "fill_01_unique.mtz", "hklout", "fill_02_cad.mtz"],
        job_dir,
        stdin="monitor BRIEF\nlabin file 1 ALL\nEND\n",
        log="fill_02_cad.log",
    )
    if not (job_dir / "fill_02_cad.mtz").is_file():
        return {"status": "error", "fatal": False, "message": "CAD failed"}

    _run_cmd(
        ["phenix.maps", str(pdb_path), "fill_02_cad.mtz"],
        job_dir, log="fill_03_maps.log",
    )
    # phenix.maps names its coefficient MTZ after the input *model* stem, so the
    # file is "final_{suffix}_map_coeffs.mtz" -- not "fill_02_cad_*".  Prefer that
    # deterministic name, then fall back to a glob to stay robust against phenix
    # naming changes (e.g. "_map_coefficients").
    dest = job_dir / f"final_{suffix}_filled.mtz"
    candidates = [job_dir / f"final_{suffix}_map_coeffs.mtz"]
    candidates += sorted(job_dir.glob("*_map_coeffs.mtz"))
    candidates += sorted(job_dir.glob("*_map_coefficients.mtz"))
    for src in candidates:
        if src.is_file():
            src.rename(dest)
            return {"status": "completed", "message": f"{dest.name} written"}

    return {"status": "error", "fatal": False,
            "message": "phenix.maps output not found"}


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
