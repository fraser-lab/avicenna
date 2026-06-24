import argparse
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
sys.path.insert(0, str(pathlib.Path(__file__).parents[2]))
from avicenna.utils.utils import format_log_line, QRefineWatcher, qsub_terse

parser = argparse.ArgumentParser(prog="vic.refine")
parser.add_argument("-d", "--data", type=pathlib.Path,
                    help="Reflection data file (MTZ)")
parser.add_argument("-m", "--model", type=pathlib.Path,
                    help="Starting model (PDB)")
parser.add_argument("-o", "--output", type=pathlib.Path,
                    default=pathlib.Path(os.curdir),
                    help="Processing directory (default: current directory)")
parser.add_argument("-l", "--list", type=pathlib.Path,
                    default=pathlib.Path("qrefine_list.txt"),
                    help="Dataset list file (default: qrefine_list.txt)")
parser.add_argument("-p", "--params", type=pathlib.Path,
                    default=pathlib.Path("qrefine_input_parameters.txt"),
                    help="MR parameter file (default: qrefine_input_parameters.txt)")
parser.add_argument("--dry_run", action="store_true",
                    help="Write scripts without submitting to SGE")

_src_dir = str(pathlib.Path(__file__).parents[3] / "src")


class QRefineParams:
    """Parse qrefine_input_parameters.txt into per-crystal MR parameter dicts.

    The file is delimited by '## crystal ##' blocks, each containing key=value
    pairs. Provides a .get(crystal_name) lookup.
    """

    def __init__(self, params_path):
        self.params_path = pathlib.Path(params_path)
        self._crystals = {}

    def parse(self):
        if not self.params_path.is_file():
            return
        text = self.params_path.read_text()
        blocks = [b.strip() for b in text.split("## crystal ##") if b.strip()]
        for block in blocks:
            params = {}
            for line in block.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, _, val = line.partition('=')
                    params[key.strip()] = val.strip()
            crystal_name = params.get('crystal_name')
            if crystal_name:
                self._crystals[crystal_name] = params

    def get(self, crystal_name):
        return self._crystals.get(crystal_name, {})


class QRefineDataset:
    """Represent one entry from qrefine_list.txt.

    Holds pin, crystal_name, cell_refined, index, and integration flag.
    Derives the input MTZ path (forced vs. non-forced) and the output
    subdirectory name under qrefine/.
    """

    def __init__(self, pin, crystal_name, cell_refined, index, integration, process_dir):
        self.pin = pin
        self.crystal_name = crystal_name
        self.cell_refined = cell_refined
        self.index = index
        self.integration = integration
        self.process_dir = pathlib.Path(process_dir)
        self.forced = None
        self.mtz_path = None
        self.job_dir = None

    @staticmethod
    def _rfree_dir(parent):
        """Locate the ``*_rfree`` step dir under *parent*.

        Don't hardcode the step number: the PHIL processing pipeline numbers
        R-free ``10_rfree`` (sequence ends ...9_aimless, 10_rfree, 11_xtriage),
        while older/legacy layouts used ``11_rfree``.  Match by suffix (as the
        scrapers do) and take the highest-numbered if several exist.  Falls back
        to the current ``10_rfree`` name when *parent* is missing (so a missing
        MTZ still surfaces as a clear path)."""
        try:
            cands = [d for d in parent.iterdir()
                     if d.is_dir() and d.name.endswith("_rfree")]
        except OSError:
            cands = []
        if cands:
            cands.sort(key=lambda d: int(d.name.split("_")[0])
                       if d.name.split("_")[0].isdigit() else 0)
            return cands[-1]
        return parent / "10_rfree"

    def resolve(self):
        crystal_dir = (f"{self.crystal_name}_{self.cell_refined}"
                       if self.cell_refined else self.crystal_name)
        index_dir = self.index.replace(' ', '_')
        base = (self.process_dir / "processing" / self.pin /
                crystal_dir / f"index_{index_dir}")
        if self.integration == 'integration not forced':
            self.forced = 'not_forced'
            parent = base
        else:
            parts = self.integration.split()
            forced_sg     = parts[3]
            forced_prefix = parts[4]
            self.forced = f"forced_{forced_sg}_{forced_prefix}"
            parent = base / f"{forced_prefix}_{forced_sg}_force"
        self.mtz_path = self._rfree_dir(parent) / "XDS_ASCII_ctruncate.mtz"
        cell_refined_part = f"_{self.cell_refined}" if self.cell_refined else ""
        self.job_dir = (
            self.process_dir / "qrefine" /
            f"{self.pin}_{self.crystal_name}{cell_refined_part}_{index_dir}_{self.forced}"
        )


class RunQRefineJob:
    """Execute the refinement pipeline for one dataset/suffix combination.

    Called by the generated run_qrefine.py worker script on a compute node.
    Mirrors the logic of qrefine_qsub_{hoh,nohoh}.sh.

    Pipeline:
      1. PDB preparation (copy or strip HOH)
      2. phenix.refine rigid body (5 cycles)
      3. phenix.phaser MR if rfree > mr_cutoff
      4. refmac5 jelly body (4 cycles)
      5. refmac5 restrained (8 cycles)
      6. Optional monomer retry if mr_try_monomer=true and phaser failed or final_rfree > 0.4
    """

    def __init__(self, params):
        self.params          = params
        self.mtz             = params["mtz"]
        self.mr_pdb          = params["mr_pdb"]
        self.mr_cutoff       = float(params["mr_cutoff"])
        self.mr_tncs         = params["mr_tncs"]
        self.mr_copies       = params["mr_copies"]
        self.mr_mw           = params["mr_mw"]
        self.mr_seq_id       = params["mr_seq_id"]
        self.mr_try_monomer  = params.get("mr_try_monomer", "false") == "true"
        self.mr_pdb_monomer  = params.get("mr_pdb_monomer", "na")
        self.mr_copies_monomer = params.get("mr_copies_monomer", "na")
        self.mr_mw_monomer   = params.get("mr_mw_monomer", "na")
        self.suffix          = params["suffix"]
        self.job_id          = params.get("job_id", params.get("pin", "?"))
        self.fill            = params.get("fill", True)

    def _run(self, cmd, stdin=None, log=None):
        result = subprocess.run(
            cmd, input=stdin, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        if log:
            pathlib.Path(log).write_text(result.stdout)
        else:
            print(result.stdout, end="", flush=True)
        return result.returncode

    def _rfree_from_log(self, log_path):
        for line in reversed(pathlib.Path(log_path).read_text().splitlines()):
            if "Final R-work =" in line:
                parts = line.split()
                try:
                    return float(parts[parts.index("R-work") + 2])
                except (ValueError, IndexError):
                    pass
        return 1.0

    def _final_rfree_from_log(self, log_path):
        lines = pathlib.Path(log_path).read_text().splitlines()
        for i, line in enumerate(lines):
            if "Final results" in line:
                tail = lines[i + 1 : i + 4]
                for tl in tail:
                    parts = tl.split()
                    if len(parts) >= 5:
                        try:
                            return float(parts[4])
                        except ValueError:
                            pass
        return 1.0

    def _log(self, tag, message):
        line = format_log_line(tag, message, context=self.job_id,
                               context_width=self.params.get("context_width"))
        with open("pipeline.log", "a") as fh:
            fh.write(line)
        print(line, end="", flush=True)

    def _pdb_stats(self, pdb_path):
        """Extract Rwork, Rfree, and resolution from REMARK 3 in a PDB file."""
        rwork = rfree = res = None
        try:
            for line in pathlib.Path(pdb_path).read_text().splitlines():
                if 'R VALUE            (WORKING SET) :' in line:
                    try:
                        rwork = float(line.split(':')[-1].strip())
                    except ValueError:
                        pass
                elif 'FREE R VALUE                     :' in line:
                    try:
                        rfree = float(line.split(':')[-1].strip())
                    except ValueError:
                        pass
                elif 'RESOLUTION RANGE HIGH (ANGSTROMS) :' in line:
                    try:
                        res = float(line.split(':')[-1].strip())
                    except ValueError:
                        pass
        except OSError:
            pass
        return rwork, rfree, res

    def _fill_reflections(self, final_pdb, final_mtz):
        """Populate missing/invalid reflections using uniqueify → CAD → phenix.maps.

        Produces final_{suffix}_filled.mtz with 2FOFCWT_fill / PH2FOFCWT_fill
        columns ready for direct panDDA input.  Strategy follows the panDDA
        documentation recommendation (phenix.maps is preferred over
        phenix.refine / dimple because it fills without re-refining the model).
        """
        s = self.suffix
        self._log("RUN", "fill: uniqueify → CAD → phenix.maps")

        # Step 1: uniqueify — generate the complete unique reflection set.
        self._run(
            ["uniqueify", "-f", "FreeR_flag", final_mtz, "fill_01_unique.mtz"],
            log="fill_01_unique.log",
        )
        if not pathlib.Path("fill_01_unique.mtz").is_file():
            self._log("WARN", "fill: uniqueify failed — skipping reflection fill")
            return

        # Step 2: CAD — populate missing reflections across the full resolution range.
        # 999.0 as the low-resolution limit ensures all shells are included.
        self._run(
            ["cad", "hklin1", "fill_01_unique.mtz", "hklout", "fill_02_cad.mtz"],
            stdin=(
                "monitor BRIEF\n"
                "labin file 1 ALL\n"
                "resolution file 1 999.0 HIGHRES\n"
                "END\n"
            ),
            log="fill_02_cad.log",
        )
        if not pathlib.Path("fill_02_cad.mtz").is_file():
            self._log("WARN", "fill: CAD failed — skipping reflection fill")
            return

        # Step 3: phenix.maps — compute 2mFo-DFc with filled coefficients.
        # Output is named <input_base>_map_coeffs.mtz by phenix.maps convention.
        self._run(
            ["phenix.maps", final_pdb, "fill_02_cad.mtz"],
            log="fill_03_maps.log",
        )
        dest = pathlib.Path(f"final_{s}_filled.mtz")
        for candidate in ("fill_02_cad_map_coeffs.mtz",
                           "fill_02_cad_map_coefficients.mtz"):
            src = pathlib.Path(candidate)
            if src.is_file():
                src.rename(dest)
                self._log("RUN", f"fill: {dest.name} written")
                return
        self._log("WARN", "fill: phenix.maps output not found — reflection fill incomplete")

    def run(self):
        s = self.suffix
        self._log("START", f"suffix={s}")

        # ------------------------------------------------------------------
        # Step 1: PDB preparation
        # ------------------------------------------------------------------
        if s == "hoh":
            shutil.copy2(self.mr_pdb, f"01-rigid-body_input_hoh.pdb")
        else:
            with open(self.mr_pdb) as fh:
                lines = [l for l in fh if not l.startswith("HETATM") or "HOH" not in l]
            pathlib.Path(f"01-rigid-body_input_nohoh.pdb").write_text("".join(lines))

        # ------------------------------------------------------------------
        # Step 2: Phenix rigid body refinement
        # ------------------------------------------------------------------
        self._run([
            "phenix.refine", f"01-rigid-body_input_{s}.pdb", self.mtz,
            "main.number_of_macro_cycles=5",
            "strategy=rigid_body",
            "write_eff_file=false", "write_geo_file=false", "write_def_file=false",
            "hydrogens.refine=riding",
            "miller_array.labels.name=F,SIGF",
            f"output.prefix=01-rigid-body_{s}",
        ])
        jelly_input = f"01-rigid-body_{s}_001.pdb"
        rfree = self._rfree_from_log(f"01-rigid-body_{s}_001.log")
        self._log("RUN", f"01-rigid-body: rfree={rfree:.4f}")

        # ------------------------------------------------------------------
        # Step 3: Phaser MR if rfree > cutoff
        # ------------------------------------------------------------------
        if rfree > self.mr_cutoff:
            if s == "hoh":
                src = pathlib.Path(self.mr_pdb).read_text().replace("HOH", "WWW")
                pathlib.Path(f"02-phaser_input_hoh.pdb").write_text(src)
            else:
                with open(self.mr_pdb) as fh:
                    lines = [l for l in fh if not l.startswith("HETATM") or "HOH" not in l]
                pathlib.Path(f"02-phaser_input_nohoh.pdb").write_text("".join(lines))

            self._run([
                "phenix.phaser", f"02-phaser_input_{s}.pdb", self.mtz,
                f"phaser.keywords.tncs.use={self.mr_tncs}",
                f"phaser.search_copies={self.mr_copies}",
                f"phaser.mol_weight={self.mr_mw}",
                f"phaser.model_identity={self.mr_seq_id}",
                f"phaser.keywords.general.root=02-phaser_{s}",
            ])

        phaser_pdb = pathlib.Path(f"02-phaser_{s}.1.pdb")
        if phaser_pdb.is_file():
            self._log("RUN", "02-phaser: success")
            jelly_input = str(phaser_pdb)
            if s == "hoh":
                text = phaser_pdb.read_text()
                text = text.replace("WWW", "HOH").replace("  0.00 ", "  1.00 ")
                phaser_pdb.write_text(text)

        # ------------------------------------------------------------------
        # Steps 4 & 5: REFMAC jelly body + restrained refinement
        # ------------------------------------------------------------------
        if phaser_pdb.is_file() or rfree < self.mr_cutoff:
            self._run(
                ["refmac5",
                 "labin", "FP=F", "SIGFP=SIGF", "FREE=FreeR_flag",
                 "XYZIN", jelly_input,
                 "HKLIN", self.mtz,
                 f"XYZOUT", f"03-jelly_{s}.pdb",
                 f"HKLOUT", f"03-jelly_{s}.mtz"],
                stdin=(
                    "make newligand continue\n"
                    "refinement type restrained\n"
                    "weight auto\n"
                    "ridge distance sigma 0.01\n"
                    "make hydrogen no\n"
                    "ncycle 4\n"
                    "END\n"
                ),
                log=f"03-jelly_{s}.log",
            )
            self._log("RUN", "03-jelly: completed")
            self._run(
                ["refmac5",
                 "labin", "FP=F", "SIGFP=SIGF", "FREE=FreeR_flag",
                 "XYZIN", f"03-jelly_{s}.pdb",
                 "HKLIN", self.mtz,
                 f"XYZOUT", f"final_{s}.pdb",
                 f"HKLOUT", f"final_{s}.mtz"],
                stdin=(
                    "make newligand continue\n"
                    "refinement type restrained\n"
                    "weight auto\n"
                    "ncycle 8\n"
                    "END\n"
                ),
                log=f"04-refmac_{s}.log",
            )
            self._log("RUN", "04-refmac: completed")
        else:
            print("Phaser failed", flush=True)

        # ------------------------------------------------------------------
        # Step 6: Optional monomer retry
        # ------------------------------------------------------------------

        if self.mr_try_monomer:
            final_rfree = self._final_rfree_from_log(f"04-refmac_{s}.log")
            if not phaser_pdb.is_file() or final_rfree > 0.4:
                self._run([
                    "phenix.phaser", self.mr_pdb_monomer, self.mtz,
                    f"phaser.keywords.tncs.use={self.mr_tncs}",
                    f"phaser.search_copies={self.mr_copies_monomer}",
                    f"phaser.mol_weight={self.mr_mw_monomer}",
                    f"phaser.model_identity={self.mr_seq_id}",
                    f"phaser.keywords.general.root=05-phaser_{s}",
                ])
                mono_pdb = pathlib.Path(f"05-phaser_{s}.1.pdb")
                if mono_pdb.is_file():
                    self._log("RUN", "05-phaser-monomer: success")
                    self._run(
                        ["refmac5",
                         "labin", "FP=F", "SIGFP=SIGF", "FREE=FreeR_flag",
                         "XYZIN", str(mono_pdb),
                         "HKLIN", self.mtz,
                         f"XYZOUT", f"06-jelly_{s}.pdb",
                         f"HKLOUT", f"06-jelly_{s}.mtz"],
                        stdin=(
                            "make newligand continue\n"
                            "refinement type restrained\n"
                            "weight auto\n"
                            "ridge distance sigma 0.01\n"
                            "make hydrogen no\n"
                            "ncycle 4\n"
                            "END\n"
                        ),
                        log=f"06-jelly_{s}.log",
                    )
                    self._log("RUN", "06-jelly-monomer: completed")
                    self._run(
                        ["refmac5",
                         "labin", "FP=F", "SIGFP=SIGF", "FREE=FreeR_flag",
                         "XYZIN", f"06-jelly_{s}.pdb",
                         "HKLIN", self.mtz,
                         f"XYZOUT", f"final_monomer_{s}.pdb",
                         f"HKLOUT", f"final_monomer_{s}.mtz"],
                        stdin=(
                            "make newligand continue\n"
                            "refinement type restrained\n"
                            "weight auto\n"
                            "ncycle 8\n"
                            "END\n"
                        ),
                        log=f"07-refmac_{s}.log",
                    )
                    self._log("RUN", "07-refmac-monomer: completed")

        # ------------------------------------------------------------------
        # Step 7: Populate missing reflections for panDDA input
        # ------------------------------------------------------------------
        if self.fill:
            for pdb_prefix in (f"final_monomer_{s}", f"final_{s}"):
                pdb_path = pathlib.Path(f"{pdb_prefix}.pdb")
                mtz_path = pathlib.Path(f"{pdb_prefix}.mtz")
                if pdb_path.is_file() and mtz_path.is_file():
                    self._fill_reflections(str(pdb_path), str(mtz_path))
                    break

        # ------------------------------------------------------------------
        # Final status
        # ------------------------------------------------------------------
        for pdb in (f"final_monomer_{s}.pdb", f"final_{s}.pdb"):
            if pathlib.Path(pdb).is_file():
                rwork, rfree_final, res = self._pdb_stats(pdb)
                parts = []
                if rwork is not None:
                    parts.append(f"Rwork={rwork:.4f}")
                if rfree_final is not None:
                    parts.append(f"Rfree={rfree_final:.4f}")
                if res is not None:
                    parts.append(f"Res={res:.2f}A")
                self._log("DONE", " ".join(parts) if parts else "completed")
                water = self.params.get("mr_water", "false")
                expected = []
                if water in ("true", "both"):
                    expected.append("hoh")
                if water in ("false", "both"):
                    expected.append("nohoh")
                if all(
                    pathlib.Path(f"final_{sx}.pdb").is_file() or
                    pathlib.Path(f"final_monomer_{sx}.pdb").is_file()
                    for sx in expected
                ):
                    self._log("COMPLETE", "all suffixes finished")
                return
        self._log("ERROR", f"no final PDB produced for suffix={s}")


class QRefineWorker:
    """Write params.json and run_qrefine.py worker scripts for one dataset.

    Generates one script per suffix (hoh, nohoh, or both). Each script
    sources SBGrid, loads params.json, and calls RunQRefineJob(params).run().
    Returns a list of paths to the generated scripts.
    """

    def __init__(self, dataset, mr_params, context_width=None, fill=True):
        self.dataset       = dataset
        self.mr_params     = mr_params
        self.context_width = context_width
        self.fill          = fill

    def _extra_params(self, suffix):
        """Per-suffix params merged into params_{suffix}.json.

        Override hook: subclasses (e.g. the PHIL interpreter orchestrator) add
        keys such as ``phil_string`` here.  Base behavior adds nothing.
        """
        return {}

    def _sbgrid_block(self):
        """Shell command lines that capture the SBGrid environment.

        Override hook: subclasses can chain additional setup scripts (e.g.
        CCP4/Phenix ``setup-sh``) after the base SBGrid sourcing.
        """
        return (
            "shell = os.environ.get('SHELL', '/bin/bash')\n"
            "if any(s in shell for s in ('csh', 'tcsh')):\n"
            "    _sbgrid_cmd = \"tcsh -c 'source /programs/sbgrid.cshrc && env'\"\n"
            "else:\n"
            "    _sbgrid_cmd = \"bash --norc --noprofile -c 'source /programs/sbgrid.shrc && env'\"\n"
        )

    def _worker_body(self, suffix):
        """Python source that loads params and runs the per-dataset job.

        Override hook: subclasses swap ``RunQRefineJob`` for the PHIL
        interpreter.  The script's CWD is the dataset job_dir at run time.
        """
        return (
            "from avicenna.pipelines.legacy.qrefine_pipeline import RunQRefineJob\n"
            "RunQRefineJob(params).run()\n"
        )

    def write(self):
        d = self.dataset
        p = self.mr_params
        scripts = []

        water = p.get('mr_water', 'false')
        suffixes = []
        if water in ('true', 'both'):
            suffixes.append('hoh')
        if water in ('false', 'both'):
            suffixes.append('nohoh')

        for suffix in suffixes:
            forced_tag = "nf" if d.forced == "not_forced" else d.forced.split("_")[1]
            job_id = f"{d.pin} | {forced_tag:<7} | {suffix:<5}"
            per_params = {**p, "mtz": str(d.mtz_path), "suffix": suffix,
                          "pin": d.pin, "job_id": job_id,
                          "context_width": self.context_width,
                          "fill": self.fill,
                          **self._extra_params(suffix)}
            script_path = d.job_dir / f"run_qrefine_{suffix}.py"

            pathlib.Path(d.job_dir / f"params_{suffix}.json").write_text(
                json.dumps(per_params, indent=2)
            )

            import_and_run = self._worker_body(suffix)
            sbgrid_block = self._sbgrid_block()
            script_path.write_text(f"""\
#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, {_src_dir!r})

{sbgrid_block}_result = subprocess.run(_sbgrid_cmd, shell=True, capture_output=True, text=True)
for _line in _result.stdout.splitlines():
    if '=' in _line:
        _key, _, _val = _line.partition('=')
        if _key.isidentifier():
            os.environ[_key] = _val

params = json.loads(Path('params_{suffix}.json').read_text())
print("=" * 60)
print(f"pin     : {{params.get('pin', 'n/a')}}")
print(f"crystal : {{params.get('crystal_name', 'n/a')}}")
print(f"mtz     : {{params.get('mtz', 'n/a')}}")
print(f"suffix  : {{params.get('suffix', 'n/a')}}")
print("=" * 60, flush=True)
{import_and_run}""")
            script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
            scripts.append(script_path)

        return scripts


class RunQRefine:
    """Orchestrate the full qrefine preparation and SGE submission.

    Reads qrefine_list.txt and qrefine_input_parameters.txt, manages the
    qrefine/ output directory, iterates over datasets, calls QRefineWorker
    for each, writes qrefine_qsub_list.txt and qrefine_qsub_submit.sh,
    and submits via qsub (unless dry_run=True).

    Also ensures CCP4 7.0 is pinned in ~/.sbgrid.conf before job generation.
    """

    def __init__(self, process_dir, dataset_list, params_file, data=None, model=None, dry_run=False, fill=True):
        self.process_dir  = pathlib.Path(process_dir).resolve()
        self.dataset_list = pathlib.Path(dataset_list)
        self.params_file  = pathlib.Path(params_file)
        self.data         = pathlib.Path(data) if data else None
        self.model        = pathlib.Path(model) if model else None
        self.dry_run      = dry_run
        self.fill         = fill

    def _pin_ccp4_version(self):
        sbgrid_conf = pathlib.Path.home() / ".sbgrid.conf"
        if not sbgrid_conf.is_file():
            return
        text = sbgrid_conf.read_text()
        if "#CCP4_X=7.0" in text:
            sbgrid_conf.write_text(text.replace("#CCP4_X=7.0", "CCP4_X=7.0"))

    def _setup_output_dir(self):
        qrefine_dir = self.process_dir / "qrefine"
        if qrefine_dir.is_dir():
            print("qrefine directory exists, old runs of the same pin will be overwritten...")
        else:
            print("Creating new qrefine directory")
            qrefine_dir.mkdir()
        qsub_list = self.process_dir / "qrefine_qsub_list.txt"
        if qsub_list.is_file():
            qsub_list.unlink()

    def _read_dataset_list(self):
        datasets = []
        with open(self.dataset_list) as fh:
            for line in fh:
                parts = [p.strip() for p in line.strip().split(',')]
                if len(parts) < 5:
                    continue
                pin, crystal_name, cell_refined, index, integration = (
                    parts[0], parts[1], parts[2],
                    parts[3].replace(' ', '_'), parts[4],
                )
                datasets.append(QRefineDataset(
                    pin, crystal_name, cell_refined, index, integration, self.process_dir
                ))
        return datasets

    def _make_worker(self, dataset, params, context_width):
        """Build the worker that writes per-suffix scripts for one dataset.

        Override hook: ``RefinementOrchestrator`` returns a worker whose scripts
        invoke the PHIL interpreter instead of ``RunQRefineJob``.
        """
        return QRefineWorker(dataset, params, context_width=context_width, fill=self.fill)

    def _write_qsub_submit(self, qsub_list, njob):
        submit_path = self.process_dir / "qrefine_qsub_submit.sh"
        logs_dir = self.process_dir / "qrefine_logs"
        logs_dir.mkdir(exist_ok=True)
        submit_path.write_text(
            "#!/bin/bash\n"
            "#$ -S /bin/bash\n"
            "#$ -cwd\n"
            f"#$ -t 1-{njob}\n"
            "#$ -j y\n"
            "#$ -pe smp 1\n"
            "#$ -l mem_free=2G\n"
            "#$ -l h_rt=02:00:00\n"
            f"#$ -o {logs_dir}\n\n"
            f'script=$(sed "${{SGE_TASK_ID}}q;d" {qsub_list})\n'
            "cd $(dirname ${script})\n"
            "./$(basename ${script})\n"
        )
        submit_path.chmod(submit_path.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
        return submit_path

    def _infer_pandda_suffix(self, mr_params):
        """Return the suffix most appropriate for panDDA from the qrefine params.

        If every crystal was run without waters (mr_water=false) the suffix is
        'nohoh'; in all other cases (hoh, both, or mixed) it is 'hoh'.
        """
        waters = {p.get("mr_water", "false") for p in mr_params._crystals.values()}
        return "nohoh" if waters == {"false"} else "hoh"

    def _write_pandda_template(self, mr_params):
        """Write pandda_template.sh in process_dir after jobs are submitted.

        The script is a complete, submittable pandda.analyse SGE job with
        FILL_IN placeholders for the three values that cannot be inferred from
        qrefine output (dataset prefix, reference PDB, reference MTZ).
        Optional arguments (ground_state_datasets, low-res completeness check)
        are included as commented-out lines.
        """
        suffix   = self._infer_pandda_suffix(mr_params)
        pdir     = self.process_dir
        pandda   = pdir / "pandda"
        data_dir = pandda / "data"
        logs_dir = pdir / "pandda_logs"

        script = pathlib.Path(pdir / "pandda_template.sh")
        script.write_text(f"""\
#!/bin/bash
# pandda_template.sh — generated by vic.refine
#
# Edit the three FILL_IN values below, then submit with:
#   qsub pandda_template.sh
#
# Required edits:
#   FILL_IN_PREFIX        — dataset name prefix, e.g. FRS
#   FILL_IN_REFERENCE_PDB — absolute path to reference PDB
#   FILL_IN_REFERENCE_MTZ — absolute path to reference MTZ
#
#$ -S /bin/bash
#$ -cwd
#$ -j y
#$ -pe smp 32
#$ -l mem_free=8G
#$ -l h_rt=24:00:00
#$ -o {logs_dir}

source /programs/sbgrid.shrc
export CCP4_X=7.0

cd {pandda}

pandda.analyse \\
  output.out_dir="{pandda}" \\
  input.data_dirs="{data_dir}/FILL_IN_PREFIX_*" \\
  input.pdb_style="final_{suffix}.pdb" \\
  input.mtz_style="final_{suffix}.mtz" \\
  input.reference.pdb="FILL_IN_REFERENCE_PDB" \\
  input.reference.mtz="FILL_IN_REFERENCE_MTZ" \\
  min_build_datasets="30" \\
  max_build_datasets="50" \\
  max_new_datasets="1500" \\
  cpus="32" \\
  high_res_increment="0.05"
  # ground_state_datasets="x0001,x0002,..."  # uncomment and fill in if needed
  # checks.low_resolution_completeness=None   # uncomment to disable LR completeness check
""")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
        print(f"Written: {script}")
        print("Edit the three FILL_IN values in that file before submitting.")

    def _load_mr_params(self):
        """Load the per-crystal MR parameter source.

        Override hook: the PHIL orchestrator returns None (it sources MR
        settings from refine.phil instead of qrefine_input_parameters.txt).
        """
        mr_params = QRefineParams(self.params_file)
        mr_params.parse()
        return mr_params

    def _validate_inputs(self, mr_params):
        """Validate reference-model paths before submission.

        Override hook: the PHIL orchestrator validates refine.input.reference_pdb.
        """
        errors = []
        for crystal_name, p in mr_params._crystals.items():
            for key in ('mr_pdb', 'mr_pdb_monomer'):
                val = p.get(key)
                if val is None or val == 'na':
                    continue
                if not pathlib.Path(val).is_file():
                    errors.append(f"  [{crystal_name}] {key}={val} — file not found")
        if errors:
            print("ERROR: qrefine_input_parameters.txt has invalid paths:")
            for e in errors:
                print(e)
            sys.exit(1)

    def _dataset_params(self, dataset, mr_params):
        """Per-dataset params dict passed to the worker.

        Override hook: the PHIL orchestrator returns PHIL-sourced values
        (water handling) rather than the per-crystal txt block.
        """
        return mr_params.get(dataset.crystal_name)

    def run(self):
        self._pin_ccp4_version()
        self._setup_output_dir()

        mr_params = self._load_mr_params()
        self._validate_inputs(mr_params)

        datasets  = self._read_dataset_list()
        qsub_list = self.process_dir / "qrefine_qsub_list.txt"

        context_width = QRefineWatcher(procpath=self.process_dir).context_width

        for dataset in datasets:
            dataset.resolve()
            print(f"{dataset.pin} {dataset.crystal_name} {dataset.cell_refined} "
                  f"{dataset.index} {dataset.forced}")

            if dataset.job_dir.is_dir():
                print(f"qrefine directory for {dataset.job_dir.name} exists, "
                      f"will delete and continue")
                shutil.rmtree(dataset.job_dir)
            dataset.job_dir.mkdir(parents=True)

            params = self._dataset_params(dataset, mr_params)
            scripts = self._make_worker(dataset, params, context_width).write()

            with open(qsub_list, 'a') as fh:
                for script in scripts:
                    fh.write(f"{script}\n")

        if not qsub_list.is_file():
            print("No jobs found — qrefine_qsub_list.txt was not created.")
            return

        njob = sum(1 for l in qsub_list.read_text().splitlines() if l.strip())
        print(f"There are {njob} jobs")

        submit_path = self._write_qsub_submit(qsub_list, njob)

        job_id = None
        if not self.dry_run:
            job_id = qsub_terse([submit_path])
            self._write_pandda_template(mr_params)
        return job_id


def main():
    args = parser.parse_args()
    RunQRefine(
        process_dir=args.output,
        dataset_list=args.list,
        params_file=args.params,
        data=args.data,
        model=args.model,
        dry_run=args.dry_run,
    ).run()


if __name__ == "__main__":
    main()
