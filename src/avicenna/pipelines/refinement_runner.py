import json
import pathlib
import shutil
import stat
import sys
sys.path.insert(0, str(pathlib.Path(__file__).parents[2]))
from avicenna.utils.utils import format_log_line, QRefineWatcher, qsub_terse
from avicenna.utils import resources

_src_dir = str(pathlib.Path(__file__).parents[3] / "src")


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


class QRefineWorker:
    """Write params.json and run_qrefine.py worker scripts for one dataset.

    Base class: ``write()`` builds one script per water suffix (hoh/nohoh) and
    delegates environment setup and the per-dataset body to three hooks
    (``_extra_params``, ``_sbgrid_block``, ``_worker_body``) provided by its
    subclass ``_InterpreterWorker``, whose scripts invoke the PHIL interpreter.
    Returns a list of paths to the generated scripts.
    """

    def __init__(self, dataset, mr_params, context_width=None, fill=True):
        self.dataset       = dataset
        self.mr_params     = mr_params
        self.context_width = context_width
        self.fill          = fill

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

    Base class for ``RefinementOrchestrator``.  Reads qrefine_list.txt, manages
    the qrefine/ output directory, iterates over datasets, calls the worker for
    each, writes qrefine_qsub_list.txt and qrefine_qsub_submit.sh, and submits
    via qsub (unless dry_run=True).  The input seams (``_load_mr_params``,
    ``_validate_inputs``, ``_dataset_params``, ``_make_worker``,
    ``_write_pandda_template``) are provided by the subclass, which sources MR
    settings from refine.phil.

    Also ensures CCP4 7.0 is pinned in ~/.sbgrid.conf before job generation.
    """

    def __init__(self, process_dir, dataset_list, data=None, model=None, dry_run=False, fill=True):
        self.process_dir  = pathlib.Path(process_dir).resolve()
        self.dataset_list = pathlib.Path(dataset_list)
        self.data         = pathlib.Path(data) if data else None
        self.model        = pathlib.Path(model) if model else None
        self.dry_run      = dry_run
        self.fill         = fill
        # SGE slots per refinement job; "auto" -> 1 (REFMAC5 is serial).  The
        # PHIL orchestrator overrides this from refine.ncpu.
        self.ncpu         = "auto"

    def _refine_slots(self):
        """Resolve the -pe smp request for a refinement job."""
        if resources.is_auto(self.ncpu):
            return resources.refine_slots()
        return int(self.ncpu)

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
            f"#$ -pe smp {self._refine_slots()}\n"
            "#$ -l mem_free=2G\n"
            "#$ -l h_rt=02:00:00\n"
            f"#$ -o {logs_dir}\n\n"
            f'script=$(sed "${{SGE_TASK_ID}}q;d" {qsub_list})\n'
            "cd $(dirname ${script})\n"
            "./$(basename ${script})\n"
        )
        submit_path.chmod(submit_path.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
        return submit_path

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
