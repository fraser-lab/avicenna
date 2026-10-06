import json
import pathlib
import shutil
import stat
import sys

from avicenna.utils import resources
from avicenna.utils.ligands import (GENERATED_DIR, find_ligand_files,
                                    generated_files, ligand_name)
from avicenna.utils.utils import qsub_terse

# Run in the pandda job before pandda.analyse: generates the ligands the
# library did not have.  The script sources SBGrid with CCP4 7.0, whose python
# cannot import this package, so the interpreter running this code is named
# outright.
_GENERATE_LIGANDS = (
    '{python} -c "import sys; sys.path.insert(0, {src!r}); '
    'from avicenna.utils.ligands import generate; generate({todo!r}, {cpus})"\n'
)


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
                 on_submit=None, project_dir=None,
                 records=None, ligand_source_path=None, ligand_libraries=None):
        self.process_dirs          = [pathlib.Path(p).resolve() for p in process_dirs]
        self.ground_state_dirs     = [pathlib.Path(p).resolve() for p in (ground_state_dirs or [])]
        # The project folder holds this stage's folder, its job logs and the run
        # lock, as it does for the other stages.
        self.project_dir           = pathlib.Path(project_dir or pathlib.Path.cwd()).resolve()
        self.pandda_dir            = self.project_dir / "pandda"
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
        # The analysed project's info.json records, by dataset tag: what each
        # dataset was soaked with.
        self.records               = records or {}
        self.ligand_source_path    = (pathlib.Path(ligand_source_path).resolve()
                                      if ligand_source_path else None)
        # None analyses every library; otherwise datasets recorded as soaked
        # from any other library are left out.
        self.ligand_libraries      = (set(ligand_libraries)
                                      if ligand_libraries is not None else None)
        # {ligand name: {"smiles", "out_dir", "targets"}} to generate in the job.
        self._to_generate          = {}
        # Called with the job id as soon as qsub returns it.
        self.on_submit             = on_submit

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

    def _recorded_jobs(self, project_dir):
        """Return (job_dir, pdb_path, mtz_path) for the refined datasets that
        *project_dir*'s info.json records, sorted by dataset tag.

        The record is the stage's input, as it is for refinement; the job
        folder is named for the dataset tag.  A dataset whose recorded model
        is missing on disk is named and skipped.  Ground-state folders are
        reference data from other projects and are still scanned instead.
        """
        from avicenna.utils.info import FILENAME, ProjectInfo

        if not (project_dir / FILENAME).is_file():
            print(f"WARNING: no {FILENAME} in {project_dir}; no datasets to analyse there.")
            return []
        info = ProjectInfo.load(str(project_dir))
        jobs = []
        for key in sorted(info.select_for_analysis()):
            record = info[key]
            pdb, mtz = record.get("final_refined_pdb"), record.get("final_refined_mtz")
            if not (pdb and mtz and pathlib.Path(pdb).is_file()
                    and pathlib.Path(mtz).is_file()):
                print(f"WARNING: {key} not analysed: its refined model is not on disk")
                continue
            job_dir = pathlib.Path(record.get("refine_job_dir")
                                   or pathlib.Path(pdb).parent).resolve()
            jobs.append((job_dir, pathlib.Path(pdb), pathlib.Path(mtz)))
        return jobs

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
        the returned mapping is in serial-number order.
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
        ligand_counts = {}
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
                found = self._place_ligands(job_dir.name, dest)
                ligand_counts[found] = ligand_counts.get(found, 0) + 1
                records.append((idx, dataset_name, job_dir, tag))

        if gs_jobs:
            codes = [f"x{idx:04d}" for idx, *_ in gs_jobs]
            print(f"\nGround-state dataset codes ({len(codes)}): {', '.join(codes)}")
        self._report_ligands(ligand_counts)

        records.sort(key=lambda r: r[0])
        return [(dn, jd, tag) for _, dn, jd, tag in records]

    def _place_ligands(self, dataset_tag, dest):
        """Put one dataset's ligand files beside its model; returns where they
        came from: "library", "generated earlier", "to generate", or None.

        Only datasets of the analysed project have a record; ground-state
        datasets, and datasets with no fragment recorded, get no ligand.
        """
        record = self.records.get(dataset_tag)
        if not record:
            return None
        library, well = record.get("library_code"), record.get("well_code")
        if not library or not well:
            return None
        pairs = find_ligand_files(self.ligand_source_path, library, well)
        found = "library"
        if not pairs:
            pairs = generated_files(self.project_dir, library, well)
            found = "generated earlier"
        if pairs:
            if not self.dry_run:
                for cif, pdb in pairs:
                    self._place_file(cif, dest / cif.name)
                    self._place_file(pdb, dest / pdb.name)
            return found
        smiles = record.get("SMILES")
        if not smiles:
            return "no SMILES"
        name = ligand_name(library, well)
        item = self._to_generate.setdefault(name, {
            "name": name, "smiles": smiles, "targets": [],
            "out_dir": str(self.project_dir / GENERATED_DIR / name)})
        item["targets"].append(str(dest))
        return "to generate"

    def _report_ligands(self, counts):
        counts = {k: v for k, v in counts.items() if k}
        if not counts:
            return
        parts = [f"{counts[k]} {label}" for k, label in (
            ("library", "from the library"),
            ("generated earlier", "generated earlier"),
            ("to generate", "to generate with phenix.elbow"),
            ("no SMILES", "with no ligand found and no SMILES to generate one"))
            if counts.get(k)]
        print(f"\nLigands: {', '.join(parts)}.")

    def _filter_libraries(self, jobs):
        """Leave out the datasets recorded as soaked from a library that was
        not selected; a dataset with no library recorded is kept."""
        if self.ligand_libraries is None:
            return jobs
        kept, left_out = [], {}
        for job in jobs:
            library = (self.records.get(job[0].name) or {}).get("library_code")
            if library and library not in self.ligand_libraries:
                left_out[library] = left_out.get(library, 0) + 1
            else:
                kept.append(job)
        if left_out:
            print("Left out by ligand_library: " + ", ".join(
                f"{n} from {lib}" for lib, n in sorted(left_out.items())))
        return kept

    def _generation_line(self):
        """The job-script line that generates the missing ligands, or "".

        The list is written beside the script; phenix.elbow is found on the
        submit host and named outright, so the job does not depend on PATH.
        """
        if not self._to_generate:
            return ""
        from avicenna.utils.software import find_phenix
        try:
            bin_dir, _version = find_phenix()
            elbow = pathlib.Path(bin_dir) / "phenix.elbow"
            if not elbow.is_file():
                raise FileNotFoundError(str(elbow))
        except (FileNotFoundError, KeyError) as exc:
            print(f"WARNING: phenix.elbow not found ({exc}); "
                  f"{len(self._to_generate)} ligand(s) will not be generated "
                  f"and those datasets are analysed without one.")
            return ""
        todo = self.pandda_dir / "ligands_todo.json"
        todo.write_text(json.dumps(
            {"elbow": str(elbow), "ligands": list(self._to_generate.values())},
            indent=1) + "\n")
        import avicenna
        src = str(pathlib.Path(avicenna.__file__).resolve().parents[1])
        return _GENERATE_LIGANDS.format(python=sys.executable, src=src,
                                        todo=str(todo), cpus=self.cpus)

    def _record_dtags(self, mapping):
        """Record each dataset's panDDA code in the analysed project's
        info.json, and clear it from that project's datasets not in this run.

        The completion sentinel traces panDDA's results back to datasets by
        this code, so a code left from an earlier run must not survive.
        """
        from avicenna.utils.info import FILENAME, ProjectInfo

        source = self.process_dirs[0]
        if not (source / FILENAME).is_file():
            print(f"WARNING: no {FILENAME} in {source}; analysis results will "
                  f"not be recorded.")
            return
        info = ProjectInfo.load(str(source))
        dtags = {job_dir.name: tag for _, job_dir, tag in mapping
                 if job_dir.parent.parent == source}
        for key, record in info.datasets.items():
            if key in dtags:
                record["pandda_dtag"] = dtags[key]
            else:
                record.pop("pandda_dtag", None)
        info.write()

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
        logs_dir  = self.project_dir / "pandda_logs"
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
            + self._generation_line()
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
            jobs = self._recorded_jobs(process_dir)
            print(f"{process_dir}: {len(jobs)} refined datasets")
            input_jobs.extend(jobs)
        input_jobs = self._filter_libraries(input_jobs)

        if not (gs_jobs or input_jobs):
            print("No refined datasets found.")
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

        self._record_dtags(mapping)
        pandda_sh = self._write_submit_script()
        print(f"Written: {pandda_sh}")
        job_id = qsub_terse([pandda_sh])
        if not job_id:
            print("WARNING: pandda.analyse was not submitted (qsub returned no job id).")
            return None
        print(f"Submitted pandda.analyse as job {job_id}")
        return self._finish(job_id)

    def _finish(self, job_id):
        """Hold the completion sentinel on *job_id*, hand both ids to the run
        lock, and follow the run; returns the sentinel's outcome, or None if
        there is none to wait for.

        The sentinel, not the pandda job, releases the lock: it is the last
        thing to run and it reads this stage's output.
        """
        from avicenna.pipelines.sentinel import AnalysisSentinel
        from avicenna.utils.utils import log_offset

        sentinel = AnalysisSentinel(self.project_dir, self.process_dirs[0])
        start = log_offset(sentinel.omnibus)
        sentinel_id = sentinel.submit(job_id, logs_dir=self.project_dir / "pandda_logs")
        if self.on_submit is not None:
            self.on_submit(job_id, sentinel_id)
        if not sentinel_id:
            print("WARNING: the completion sentinel was not submitted; the "
                  "results will not be recorded in info.json.")
            return None
        return sentinel.follow(start=start, log_time_out=self.log_time_out)
