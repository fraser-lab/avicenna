import pathlib
import shutil
import stat
import subprocess
import time

_src_dir = str(pathlib.Path(__file__).parents[3] / "src")

from avicenna.utils import resources


class PanDDAPreparer:
    """Prepare pandda input from qrefine output and submit pandda.analyse.

    Scans one or more process_dirs for completed qrefine jobs (final_{suffix}.pdb
    and final_{suffix}.mtz present), copies files into pandda/data/{prefix}_x{nnnn}/,
    writes run_pandda.py and pandda_qsub.sh, and submits to SGE.
    """

    def __init__(self, process_dirs, prefix="FRS", offset=0, suffix="hoh",
                 reference_pdb=None, reference_mtz=None,
                 ground_state_datasets=None, cpus=32,
                 min_build_datasets=30, max_build_datasets=50,
                 max_new_datasets=1500, high_res_increment=0.05,
                 no_lr_comp_check=False, log_time_out=0, dry_run=False,
                 pin_map=None):
        self.process_dirs          = [pathlib.Path(p).resolve() for p in process_dirs]
        self.pandda_dir            = pathlib.Path.cwd() / "pandda"
        self.prefix                = prefix
        self.offset                = offset
        self.suffix                = suffix
        self.reference_pdb         = pathlib.Path(reference_pdb).resolve() if reference_pdb else None
        self.reference_mtz         = pathlib.Path(reference_mtz).resolve() if reference_mtz else None
        self.ground_state_datasets = list(ground_state_datasets or [])
        self.cpus                  = cpus
        self.min_build_datasets    = min_build_datasets
        self.max_build_datasets    = max_build_datasets
        self.max_new_datasets      = max_new_datasets
        self.high_res_increment    = high_res_increment
        self.no_lr_comp_check      = no_lr_comp_check
        self.log_time_out          = log_time_out
        self.dry_run               = dry_run
        self.pin_map               = pin_map or {}

    def _scan_qrefine(self, process_dir):
        """Return sorted list of (job_dir, pdb_path, mtz_path) for completed jobs."""
        qrefine_dir = process_dir / "qrefine"
        if not qrefine_dir.is_dir():
            print(f"Warning: {qrefine_dir} is not a directory")
            return []
        results = []
        for job_dir in sorted(qrefine_dir.iterdir()):
            if not job_dir.is_dir():
                continue
            pdb = job_dir / f"final_{self.suffix}.pdb"
            mtz = job_dir / f"final_{self.suffix}.mtz"
            if pdb.is_file() and mtz.is_file():
                results.append((job_dir, pdb, mtz))
        return results

    def _prepare_data_dir(self, all_jobs):
        """Copy PDB/MTZ for each job into pandda/data/{prefix}_x{nnnn}/."""
        data_dir = self.pandda_dir / "data"
        if not self.dry_run:
            data_dir.mkdir(parents=True, exist_ok=True)
        mapping = []
        idx = 1 + self.offset
        for job_dir, pdb, mtz in all_jobs:
            tag         = f"x{idx:04d}"
            dataset_name = f"{self.prefix}_{tag}"
            dest = data_dir / dataset_name
            if not self.dry_run:
                print(f"  {job_dir.name}  ->  {dataset_name}")
            if not self.dry_run:
                dest.mkdir(exist_ok=True)
                shutil.copy2(pdb, dest / pdb.name)
                shutil.copy2(mtz, dest / mtz.name)
            pin_code = next(
                (code for full_pin, code in self.pin_map.items()
                 if job_dir.name.startswith(full_pin + '_')),
                job_dir.name
            )
            mapping.append((dataset_name, str(job_dir), tag, pin_code))
            idx += 1
        return mapping

    def _write_info_csv(self, mapping):
        csv_path = self.pandda_dir / "info.csv"
        with open(csv_path, "w") as fh:
            for dataset_name, job_dir, _, pin_code in mapping:
                fh.write(f"{pin_code},{dataset_name},{job_dir}\n")

    def _pandda_args(self):
        data_dir = self.pandda_dir / "data"
        args = [
            f'output.out_dir="{self.pandda_dir}"',
            f'input.data_dirs="{data_dir}/{self.prefix}_*"',
            f'input.pdb_style="final_{self.suffix}.pdb"',
            f'input.mtz_style="final_{self.suffix}.mtz"',
            f'min_build_datasets="{self.min_build_datasets}"',
            f'max_build_datasets="{self.max_build_datasets}"',
            f'max_new_datasets="{self.max_new_datasets}"',
            f'cpus="{self.cpus}"',
            f'high_res_increment="{self.high_res_increment}"',
        ]
        if self.no_lr_comp_check:
            args.append('checks.low_resolution_completeness=None')
        if self.reference_pdb:
            args.append(f'input.reference.pdb="{self.reference_pdb}"')
        if self.reference_mtz:
            args.append(f'input.reference.mtz="{self.reference_mtz}"')
        if self.ground_state_datasets:
            args.append(f'ground_state_datasets="{",".join(self.ground_state_datasets)}"')
        return args

    def _write_submit_script(self):
        pandda_sh = self.pandda_dir / "pandda.sh"
        logs_dir  = self.pandda_dir.parent / "pandda_logs"
        logs_dir.mkdir(exist_ok=True)

        args     = self._pandda_args()
        pandda_cmd = "pandda.analyse \\\n" + " \\\n".join(args) + "\n"

        pandda_sh.write_text(
            "#!/bin/bash\n"
            "#$ -S /bin/bash\n"
            "#$ -cwd\n"
            "#$ -j y\n"
            f"#$ -pe smp {self.cpus}\n"
            "#$ -l mem_free=8G\n"
            "#$ -l h_rt=24:00:00\n"
            f"#$ -o {logs_dir}\n\n"
            "source /programs/sbgrid.shrc\n"
            "CCP4_X=7.0\n\n"
            f"cd {self.pandda_dir}\n\n"
            + pandda_cmd
        )
        pandda_sh.chmod(pandda_sh.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
        return pandda_sh

    def run(self):
        all_jobs = []
        for process_dir in self.process_dirs:
            jobs = self._scan_qrefine(process_dir)
            print(f"{process_dir}: {len(jobs)} completed jobs")
            all_jobs.extend(jobs)

        if not all_jobs:
            print("No completed qrefine jobs found.")
            return

        mapping = self._prepare_data_dir(all_jobs)

        # Resolve cpus now that the dataset count is known: PanDDA parallelises
        # across datasets, so auto requests one core per dataset (capped).
        if resources.is_auto(self.cpus):
            self.cpus = resources.pandda_cpus(len(mapping))
            print(f"Auto-set pandda.analyse cpus = {self.cpus} (for {len(mapping)} datasets)")

        if self.dry_run:
            gs_set = set(self.ground_state_datasets)
            gs = [(name, job) for name, job, tag, _ in mapping if tag in gs_set]
            lb = [(name, job) for name, job, tag, _ in mapping if tag not in gs_set]
            if gs:
                print(f"\nGround state datasets ({len(gs)}):")
                for name, job in gs:
                    print(f"  {name}  <-  {pathlib.Path(job).name}")
            label = "Ligand-bound datasets" if gs else "Datasets"
            print(f"\n{label} ({len(lb)}):")
            for name, job in lb:
                print(f"  {name}  <-  {pathlib.Path(job).name}")
            print("\npandda.analyse command:")
            print("pandda.analyse \\\n" + " \\\n".join(f"  {a}" for a in self._pandda_args()))
            return

        print(f"Prepared {len(mapping)} datasets in {self.pandda_dir / 'data'}")

        self._write_info_csv(mapping)
        pandda_sh = self._write_submit_script()
        print(f"Written: {pandda_sh}")
        subprocess.run(["qsub", str(pandda_sh)])
        self._tail_pandda_log()

    def _tail_pandda_log(self):
        logs_dir = self.pandda_dir / "logs"
        print(f"Waiting for pandda log in {logs_dir} ...")
        start = time.time()
        while True:
            logs = sorted(logs_dir.glob("pandda-*.log"), key=lambda p: p.stat().st_mtime)
            if logs:
                log_path = logs[-1]
                print(f"Tailing {log_path.name}")
                try:
                    subprocess.run(["tail", "-n", "+1", "-f", str(log_path)])
                except KeyboardInterrupt:
                    print("\nStopped tailing.")
                return
            if self.log_time_out > 0 and (time.time() - start) >= self.log_time_out * 60:
                print("Timed out waiting for pandda log.")
                return
            time.sleep(5)
