import pathlib
import shutil
import stat
import subprocess
import time

_src_dir = str(pathlib.Path(__file__).parents[3] / "src")

from avicenna.utils import resources


class PanDDAPreparer:
    """Prepare pandda input from qrefine output and submit pandda.analyse.

    Scans one or more process_dirs for completed qrefine jobs (the canonical
    final_refined.pdb and final_refined.mtz present), copies files into
    pandda/data/{prefix}_x{nnnn}/, writes run_pandda.py and pandda_qsub.sh, and
    submits to SGE.
    """

    def __init__(self, process_dirs, prefix="FRS", offset="auto",
                 reference_pdb=None, reference_mtz=None,
                 ground_state_datasets=None, ground_state_dirs=None,
                 ground_state_range="auto", copy_input=True, remove_waters=False,
                 cpus=32, min_build_datasets=30, max_build_datasets=50,
                 max_new_datasets=1500, high_res_increment=0.05,
                 no_lr_comp_check=False, log_time_out=0, dry_run=False,
                 pin_map=None):
        self.process_dirs          = [pathlib.Path(p).resolve() for p in process_dirs]
        self.ground_state_dirs     = [pathlib.Path(p).resolve() for p in (ground_state_dirs or [])]
        self.pandda_dir            = pathlib.Path.cwd() / "pandda"
        self.prefix                = prefix
        self.offset                = offset
        self.ground_state_range    = ground_state_range
        self.copy_input            = copy_input
        self.remove_waters         = remove_waters
        self.reference_pdb         = pathlib.Path(reference_pdb).resolve() if reference_pdb else None
        self.reference_mtz         = pathlib.Path(reference_mtz).resolve() if reference_mtz else None
        # Resolved in run() (auto -> from the ground-state folder, or the manual
        # range).  A caller may still seed it explicitly as a tag list.
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
        """Return sorted list of (job_dir, pdb_path, mtz_path) for completed jobs.

        Keys off the canonical output the refine sub-pipeline publishes
        (final_refined.pdb + final_refined.mtz), not a per-step / per-water-suffix
        name: refine always emits this single stable pair regardless of which
        step ran last or whether waters were kept.
        """
        qrefine_dir = process_dir / "qrefine"
        if not qrefine_dir.is_dir():
            print(f"Warning: {qrefine_dir} is not a directory")
            return []
        results = []
        for job_dir in sorted(qrefine_dir.iterdir()):
            if not job_dir.is_dir():
                continue
            pdb = job_dir / "final_refined.pdb"
            mtz = job_dir / "final_refined.mtz"
            if pdb.is_file() and mtz.is_file():
                results.append((job_dir, pdb, mtz))
        return results

    def _place_file(self, src, dest):
        """Put a single PDB/MTZ at *dest*: copy when copy_input, else symlink.

        Symlinks point at the absolute source path (job dirs are resolved), so
        they survive a change of working directory -- but, as with any symlink,
        they break if the original file is moved or deleted.
        """
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        if self.copy_input:
            shutil.copy2(src, dest)
        else:
            dest.symlink_to(src)

    def _place_pdb(self, src, dest):
        """Place the model PDB at *dest*, stripping ordered solvent when
        remove_waters is set.

        A water-free input model changes how pandda.analyse builds its event
        maps; producing it here -- rather than by refining without waters --
        keeps ordered solvent in refinement (where it helps, especially at high
        resolution) while still giving panDDA the water-free model.  Only HOH
        coordinate records are removed (ATOM/HETATM/ANISOU), matching the legacy
        'nohoh' semantics; ligands, ions, and structural cofactors are kept.

        When stripping, a real (filtered) file is always written even in
        symlink mode (copy_input=False) -- a symlink cannot carry the edit.
        Otherwise defers to _place_file (copy or symlink).
        """
        if not self.remove_waters:
            self._place_file(src, dest)
            return
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        kept = [ln for ln in pathlib.Path(src).read_text().splitlines(keepends=True)
                if not (ln.startswith(("ATOM", "HETATM", "ANISOU"))
                        and ln[17:20].strip() == "HOH")]
        dest.write_text("".join(kept))

    def _prepare_data_dir(self, indexed_jobs):
        """Place PDB/MTZ into pandda/data/{prefix}_x{nnnn}/ for pre-indexed jobs,
        printing each dataset as it is placed under a ground-state / to-analyze
        header, then the ground-state codes.

        Files are copied when ``copy_input`` is True (default) or symlinked when
        False (saves space).  Datasets are placed ground-state-first so the live
        listing is grouped; ``indexed_jobs`` is ``(idx, job_dir, pdb, mtz)`` and
        the returned mapping is in serial-number order (for a tidy info.csv).
        The split is by the flagged ground-state set (what pandda is told), not
        physical origin, so an unflagged apo dataset lists under "to analyze".
        """
        data_dir = self.pandda_dir / "data"
        if not self.dry_run:
            data_dir.mkdir(parents=True, exist_ok=True)
        gs_set = set(self.ground_state_datasets)
        verb = ("copied" if self.copy_input else "symlinked") if not self.dry_run \
            else ("to copy" if self.copy_input else "to symlink")
        if self.remove_waters:
            print("Ordered solvent (HOH) will be removed from each input model.")

        gs_jobs = [t for t in indexed_jobs if f"x{t[0]:04d}" in gs_set]
        an_jobs = [t for t in indexed_jobs if f"x{t[0]:04d}" not in gs_set]

        records = []
        for label, group in (("Ground-state datasets", gs_jobs),
                             ("Datasets to analyze", an_jobs)):
            if label.startswith("Ground-state") and not group:
                continue            # no ground state -> skip the header entirely
            print(f"\n{label} ({len(group)}, {verb}):")
            for idx, job_dir, pdb, mtz in group:
                tag         = f"x{idx:04d}"
                dataset_name = f"{self.prefix}_{tag}"
                dest = data_dir / dataset_name
                print(f"  {dataset_name}  <-  {job_dir.name}")
                if not self.dry_run:
                    dest.mkdir(exist_ok=True)
                    self._place_pdb(pdb, dest / pdb.name)
                    self._place_file(mtz, dest / mtz.name)
                pin_code = next(
                    (code for full_pin, code in self.pin_map.items()
                     if job_dir.name.startswith(full_pin + '_')),
                    job_dir.name
                )
                records.append((idx, dataset_name, str(job_dir), tag, pin_code))

        if gs_jobs:
            codes = [f"x{idx:04d}" for idx, *_ in gs_jobs]
            print(f"\nGround-state dataset codes ({len(codes)}): {', '.join(codes)}")

        records.sort(key=lambda r: r[0])
        return [(dn, jd, tag, pc) for _, dn, jd, tag, pc in records]

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
            'input.pdb_style="final_refined.pdb"',
            'input.mtz_style="final_refined.mtz"',
            # NB: structure_factors is intentionally NOT set.  panDDA's own
            # default column list (params.diffraction_data.structure_factors =
            # FWT,PHWT 2FOFCWT_fill,PH2FOFCWT_fill 2FOFCWT,PH2FOFCWT) already does
            # the right thing and is more robust than any override we could hard-
            # code: for each dataset it uses the first pair PRESENT, so the filled
            # canonical MTZ (final_refined.mtz carries 2FOFCWT_fill/PH2FOFCWT_fill
            # -- complete to low resolution, no FWT/PHWT) resolves to the filled
            # columns and passes the low_resolution_completeness check, which is
            # exactly the orphaned-fill fix.  The breadth of the default also
            # matches the user's reference MTZ, whose columns we do not control --
            # a narrow override (e.g. only the fill pair) makes the REFERENCE
            # dataset fail its structure-factor check.  Verified end-to-end
            # against pandda.analyse (CCP4 7.0) on the filled datasets + apo
            # reference.  (The escape hatch for genuinely incomplete data remains
            # pandda.analyse.no_lr_comp_check.)
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

    @staticmethod
    def _parse_range(spec):
        """Parse 'lo-hi' (each side optionally x-prefixed, e.g. 'x0001-x0030' or
        '1-30') to (lo, hi) ints, or None if it cannot be parsed."""
        try:
            lo, hi = (int(part.strip().lower().lstrip("x"))
                      for part in str(spec).split("-"))
        except (ValueError, AttributeError):
            return None
        return (lo, hi)

    def run(self):
        # Ground-state (apo) datasets are collected FIRST so they occupy the
        # leading {prefix}_x0001..xN slots; the ligand-bound (input) datasets
        # follow, starting at offset+1.
        gs_jobs = []
        for gs_dir in self.ground_state_dirs:
            jobs = self._scan_qrefine(gs_dir)
            print(f"{gs_dir}: {len(jobs)} completed ground-state jobs")
            gs_jobs.extend(jobs)

        input_jobs = []
        for process_dir in self.process_dirs:
            jobs = self._scan_qrefine(process_dir)
            print(f"{process_dir}: {len(jobs)} completed jobs")
            input_jobs.extend(jobs)

        if not (gs_jobs or input_jobs):
            print("No completed qrefine jobs found.")
            return

        n_gs = len(gs_jobs)

        # --- Resolve offset (the demarcation: ligand datasets start at offset+1).
        # auto -> the number of ground-state datasets found (contiguous packing).
        # A user value supersedes the calculation but must leave room for the
        # ground-state block (offset >= n_gs), else the blocks would collide.
        if resources.is_auto(self.offset):
            offset = n_gs
        else:
            offset = int(self.offset)
            if not gs_jobs and offset:
                print(f"Warning: offset={offset} applied but no ground-state "
                      f"datasets were provided; the leading {offset} dataset "
                      "slots will be left empty.")
            if offset < n_gs:
                raise ValueError(
                    f"offset ({offset}) is smaller than the number of "
                    f"ground-state datasets found ({n_gs}); the ligand datasets "
                    "would collide with the ground-state block. Raise offset or "
                    "leave it on auto.")
        self.offset = offset

        # --- Physical slot assignment.
        #   ground-state (apo): 1 .. n_gs
        #   ligand (input):     offset+1 .. offset+n_lig
        indexed = [(i, *job) for i, job in enumerate(gs_jobs, start=1)]
        indexed += [(i, *job) for i, job in enumerate(input_jobs, start=offset + 1)]

        # --- Resolve ground_state_range -> the tags flagged to pandda.analyse.
        if not gs_jobs:
            # Nothing to flag; a manual range without a ground-state folder is
            # meaningless, so ignore it (with a warning).
            if not resources.is_auto(self.ground_state_range):
                print(f"Warning: ground_state_range={self.ground_state_range} "
                      "ignored because no ground-state datasets were provided.")
            self.ground_state_datasets = []
            self.ground_state_range = None
        elif resources.is_auto(self.ground_state_range):
            # auto -> flag every ground-state dataset found.
            lo, hi = 1, n_gs
            self.ground_state_datasets = [f"x{i:04d}" for i in range(lo, hi + 1)]
            self.ground_state_range = f"x{lo:04d}-x{hi:04d}"
        else:
            parsed = self._parse_range(self.ground_state_range)
            if parsed is None:
                raise ValueError(
                    f"could not parse ground_state_range="
                    f"{self.ground_state_range!r}; expected 'lo-hi' (e.g. 1-30 "
                    "or x0001-x0030).")
            lo, hi = parsed
            if hi > offset:
                raise ValueError(
                    f"ground_state_range upper bound (x{hi:04d}) exceeds the "
                    f"offset/demarcation (x{offset:04d}); it would flag ligand "
                    "datasets as ground state. Reduce the range or raise offset.")
            if (lo, hi) != (1, n_gs):
                print(f"Warning: ground_state_range x{lo:04d}-x{hi:04d} does not "
                      f"match the {n_gs} ground-state datasets found "
                      f"(x0001-x{n_gs:04d}); some ground-state datasets may be "
                      "analysed as ligand datasets, or empty slots flagged.")
            self.ground_state_datasets = [f"x{i:04d}" for i in range(lo, hi + 1)]
            self.ground_state_range = f"x{lo:04d}-x{hi:04d}"

        # Resolve cpus now that the dataset count is known: PanDDA parallelises
        # across datasets, so auto requests one core per dataset (capped).
        if resources.is_auto(self.cpus):
            self.cpus = resources.pandda_cpus(len(indexed))
            print(f"Auto-set pandda.analyse cpus = {self.cpus} (for {len(indexed)} datasets)")

        # Place the datasets, printing each one (grouped) as it goes.
        mapping = self._prepare_data_dir(indexed)

        if self.dry_run:
            print("\npandda.analyse command:")
            print("pandda.analyse \\\n" + " \\\n".join(f"  {a}" for a in self._pandda_args()))
            return

        print(f"\nPrepared {len(mapping)} datasets in {self.pandda_dir / 'data'} "
              f"({'copied' if self.copy_input else 'symlinked'}).")

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
