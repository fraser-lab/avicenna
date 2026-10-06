import contextlib
import io
import os
import sys
import argparse
import pathlib
import time
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import (QRefineWatcher, QRefineScraper, QRefineWriter,
                                 ProgressBar, log_phil_diff, log_offset,
                                 clear_stage_outputs,
                                 EXIT_FAILURE, EXIT_RUN_ACTIVE, EXIT_RUN_COMPLETE,
                                 EXIT_RUN_INCOMPLETE,
                                 SENTINEL_WAIT_FAILED, SENTINEL_WAIT_TIMEOUT)
from avicenna.utils.runlock import RunLock, RunLockHeld
from avicenna.pipelines.gather import reset_refinement
from avicenna.pipelines.refinement import RefinementOrchestrator
from avicenna.pipelines.refinement_runner import load_datasets
from avicenna.pipelines.sentinel import RefinementSentinel
from avicenna.pipelines.survey import RUN_COMPLETE, RUN_NONE, survey_refine
from avicenna.utils.info import FILENAME, ProjectInfo
from avicenna.utils.phil import RefinePHIL
from avicenna.utils.software import check_programs

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

_DEFAULT_PHIL  = "refine.phil"          # effective settings, written each run
_TEMPLATE_PHIL = "refine_default.phil"  # defaults template, written by --dry_run

parser = argparse.ArgumentParser(prog="vic.refine")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument("--settings", type=pathlib.Path, metavar="FILE",
                    help=f"PHIL settings file (default: auto-load {_DEFAULT_PHIL} if present)")
parser.add_argument("-o", "--output", type=pathlib.Path,
                    default=pathlib.Path(os.curdir),
                    help="Processing directory (default: current directory)")
parser.add_argument("-l", "--list", type=pathlib.Path, default=None,
                    help="Developer override: refine the rows of this qrefine_list.txt "
                         "instead of the processing winners in info.json")
parser.add_argument("--dry_run", action="store_true",
                    help=f"Write qrefine_list.txt from the info.json winners and {_TEMPLATE_PHIL} without submitting jobs")
parser.add_argument("--silent", action="store_true",
                    help="Submit jobs without tailing the log")
parser.add_argument("--no-prog-log", dest="no_prog_log", action="store_true",
                    help="Disable the multiline progress bar; show plain progress updates instead")
parser.add_argument("--no_fill", action="store_true",
                    help="Skip populating missing/invalid reflections in the final MTZ (not recommended for panDDA input)")
parser.add_argument("--ignore_lock", action="store_true",
                    help="Start even if another run holds the lock on the processing directory")
_rerun_group = parser.add_mutually_exclusive_group()
_rerun_group.add_argument("--resume", action="store_true",
                          help="Complete an incomplete run in the output folder")
_rerun_group.add_argument("--overwrite", action="store_true",
                          help="Discard this stage's results in the output folder and start over")
_phil_group = parser.add_mutually_exclusive_group()
_phil_group.add_argument("-c", dest="phil", action="store_true",
                         help="Print refinement PHIL parameters and exit")
_phil_group.add_argument("-e", dest="phil_expert_level", type=int, metavar="LEVEL",
                         help="Print refinement PHIL parameters up to expert level LEVEL and exit")
parser.add_argument("-a", dest="phil_attrs", action="store_true",
                    help="Show PHIL parameter attributes (use with -c or -e)")


def _apply_phil_file(phil_obj, path: pathlib.Path):
    try:
        phil_obj.from_file(str(path))
        print(f"Loaded settings from {path}")
    except RuntimeError as e:
        sys.exit(f"ERROR: Invalid PHIL in {path}:\n  {e}")


# What --overwrite removes from the project directory: this stage's results and
# logs.  Settings (.phil, including the analysis.phil this stage emits) and the
# dataset list are kept, so a restart with changed settings can happen in the
# same folder; other stages' results are not this stage's to remove.
_OVERWRITE_DIRS  = ("qrefine", "qrefine_logs")
_OVERWRITE_FILES = ("qrefine_qsub_list.txt", "qrefine_qsub_submit.sh",
                    "sentinel_refine.py", "sentinel_refine_submit.sh",
                    "info.csv")
_OVERWRITE_GLOBS = ("sentinel_refine_submit.sh.o*",)


def _clear_refine(project_dir):
    """Remove what an earlier refinement run left, keeping its log as a backup."""
    clear_stage_outputs(project_dir, "qrefine.log", _OVERWRITE_DIRS,
                        _OVERWRITE_FILES, _OVERWRITE_GLOBS)


def _reset_refine_records(project_dir):
    """Take the deleted refinement results out of info.json as well."""
    if not os.path.isfile(os.path.join(project_dir, FILENAME)):
        return
    info = ProjectInfo.load(project_dir)
    reset = reset_refinement(info)
    if not reset:
        return
    try:
        info.write()
    except OSError as exc:
        print(f"WARNING: could not write {info.path}: {exc}")
        return
    print(f"Reset {len(reset)} refined dataset(s) in {FILENAME} to processed")


def _existing_results(project_dir, dataset_list=None, resume=False, overwrite=False):
    """Deal with what an earlier run left in *project_dir*.

    Returns None to go ahead, or the exit status to stop with.  Stopping writes
    nothing, so the folder is left as the earlier run left it.  Unfinished jobs
    need no clearing here: the orchestrator replaces every job folder it
    submits, and on resume it keeps only the finished ones.
    """
    if overwrite:
        _clear_refine(project_dir)
        _reset_refine_records(project_dir)
        return None
    survey = survey_refine(project_dir, dataset_list)
    if survey.state == RUN_NONE:
        return None
    if survey.state == RUN_COMPLETE:
        if resume:
            print(f"All {survey.total} datasets already refined; nothing to resume.")
            return 0
        print("WARNING: Results found for all data. Either re-run with --overwrite "
              "option or create a new folder.")
        return EXIT_RUN_COMPLETE
    if not resume:
        print(f"WARNING: Results found for {survey.done}/{survey.total} datasets. "
              "Re-run with --overwrite option or create a new folder to start over "
              "or re-run with --resume to complete current run.")
        return EXIT_RUN_INCOMPLETE
    return None


def _watch_with_bar(watcher, omnibus):
    """Tail per-job pipeline logs with a compact progress bar pinned below.

    Captures new log lines to a buffer; only erases the bar (which causes a
    visible blink) when there is content to splice in above it.  Otherwise the
    bar's draw() does a spinner-only in-place update.  The braille spinner in
    the title line animates each tick so a frozen terminal is visible.
    """
    bar = ProgressBar(watcher, multiline=True, separator=True)
    while True:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            watcher.tail_logs(log_file=omnibus)
        new_log = buf.getvalue()
        if new_log:
            bar.erase()
            sys.stdout.write(new_log)
            sys.stdout.flush()
        still_running = bar.draw()
        if not still_running:
            break
        time.sleep(1)


def main():
    lock = None
    try:
        args, _extras = parser.parse_known_args()

        if args.phil or args.phil_expert_level is not None or args.phil_attrs:
            sys.stdout.write(RefinePHIL().full_phil_string(
                expert_level=args.phil_expert_level,
                attributes=args.phil_attrs,
            ))
            sys.exit(0)

        phil = RefinePHIL()
        if args.settings is not None:
            if not args.settings.exists():
                parser.error(f"Settings file not found: {args.settings}")
            _apply_phil_file(phil, args.settings)
        elif pathlib.Path(_DEFAULT_PHIL).exists():
            _apply_phil_file(phil, pathlib.Path(_DEFAULT_PHIL))

        # PHIL overrides given as CLI args (e.g. water=hoh reference_pdb=ref.pdb).
        _flags = [e for e in _extras if e.startswith("-")]
        if _flags:
            parser.error("unrecognized arguments: " + " ".join(_flags))
        if _extras:
            try:
                phil.adopt_cli_args(_extras)
                print("Applied PHIL overrides: " + " ".join(_extras))
            except ValueError as e:
                sys.exit(f"ERROR: {e}")

        # Inject the resolved run paths so the written PHIL is self-contained.
        _input_snap = f"""\
refine {{
  input {{
    output_dir = "{pathlib.Path(args.output).resolve()}"
    dataset_list = {f'"{args.list}"' if args.list else "None"}
  }}
}}"""

        if args.dry_run:
            QRefineWriter(args.output).write()
            phil.adopt_phil_string(_input_snap)
            with open(_TEMPLATE_PHIL, "w") as _f:
                _f.write(phil.full_phil_string())
            print(f"Written: {_TEMPLATE_PHIL}")
            return

        # Software check only on a real run (dry_run never needs the programs).
        _steps = phil._params.refine.steps.step
        check_programs(phil._params.refine.programs,
                       used_programs={s.program for s in _steps},
                       program_choices=set(phil._program_blocks))

        # Refuse before anything is locked or written when there is nothing
        # to refine, or the override list cannot be used.
        try:
            if not load_datasets(os.path.abspath(args.output), args.list,
                                 report=True):
                source = args.list or os.path.join(args.output, "info.json")
                sys.exit(f"ERROR: no datasets to refine in {source}")
        except (OSError, ValueError) as e:
            sys.exit(f"ERROR: {e}")

        # The sentinel runs from the project directory, not from here.
        list_path = os.path.abspath(args.list) if args.list else None
        sentinel = None

        # One run at a time in a project directory, whichever sub-pipeline it
        # is: refinement reads what processing's sentinel writes.  --dry_run
        # submits nothing and returned above.
        lock = RunLock(os.path.abspath(args.output), "refine")
        try:
            lock.acquire(ignore=args.ignore_lock)
        except RunLockHeld as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(EXIT_RUN_ACTIVE if exc.active else EXIT_FAILURE)
        stop = _existing_results(os.path.abspath(args.output), args.list,
                                 resume=args.resume, overwrite=args.overwrite)
        if stop is not None:
            lock.release()
            sys.exit(stop)
        log_start = log_offset(os.path.join(os.path.abspath(args.output), "qrefine.log"))

        if args.no_fill:
            # Disable any fill step in the PHIL so the interpreter skips it.
            fill_ids = [s.id for s in phil._params.refine.steps.step
                        if s.program == "fill"]
            steps = "".join(
                f"    step {{\n"
                f"      id = {sid}\n"
                f"      program = fill\n"
                f"      fill {{ enabled = False }}\n"
                f"    }}\n"
                for sid in fill_ids
            )
            if steps:
                phil.adopt_phil_string(f"refine {{\n  steps {{\n{steps}  }}\n}}\n")
        # Snapshot the effective PHIL for this run (after CLI overrides), with
        # the resolved run paths injected.  Written on every run, even when
        # identical to the defaults; refine_default.phil stays the template.
        phil.adopt_phil_string(_input_snap)
        with open(_DEFAULT_PHIL, "w") as _f:
            _f.write(phil.full_phil_string())
        print(f"Wrote {_DEFAULT_PHIL} (effective settings for this run)")
        log_phil_diff(phil, os.path.join(os.path.abspath(args.output), "qrefine.log"))
        main_job_id = RefinementOrchestrator(
            process_dir=args.output,
            dataset_list=args.list,
            dry_run=False,
            fill=not args.no_fill,
            phil_string=phil.diff_phil_string(),
            resume=args.resume,
        ).run()
        # Finalization (scrape + analysis.phil) runs on job completion via an
        # SGE sentinel, so it survives --silent / a dropped connection.
        if main_job_id:
            sentinel = RefinementSentinel(os.path.abspath(args.output),
                                          dataset_list=list_path)
            sentinel_job_id = sentinel.submit(main_job_id)
            lock.record_jobs(main_job_id, sentinel_job_id)
        else:
            lock.release()

        if args.silent:
            return

        procpath = os.path.abspath(args.output)
        omnibus = os.path.join(procpath, "qrefine.log")
        watcher = QRefineWatcher(procpath=procpath, dataset_list=list_path)
        if args.no_prog_log:
            while True:
                watcher.tail_logs(log_file=omnibus)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    still_running = watcher.show_progress()
                if not still_running:
                    break
                time.sleep(1)
        else:
            _watch_with_bar(watcher, omnibus)
        watcher.tail_logs(log_file=omnibus)
        print()
        # Wait for the held SGE sentinel to record completion (it also writes
        # analysis.phil) before declaring the run done.  Safe to Ctrl-C.
        finalized = True
        if sentinel is not None:
            outcome = sentinel.wait(start=log_start)
            if outcome == SENTINEL_WAIT_FAILED:
                finalized = False
                print("ERROR: the completion sentinel failed to finalize this run; "
                      "analysis.phil may be missing or incomplete.")
                print(f"The sentinel's log is in {procpath}.")
            elif outcome == SENTINEL_WAIT_TIMEOUT:
                print("Sentinel marker not seen within the timeout; it will still "
                      "finalize on the cluster (analysis.phil).")
        watcher.print_summary(omnibus=omnibus)
        # Short-form summary to the main log; the full table is a deep-analysis
        # tool available via `vic.summary -p refine -V`.
        scraper = QRefineScraper(procpath)
        scraper.scrape_all()
        scraper.print_summary(omnibus=omnibus)
        if not finalized:
            sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted. Any submitted jobs are still running on the cluster.")
        # An interrupt before submission leaves a lock nothing will ever
        # release; after it, the cluster jobs still hold the directory.
        if lock is not None:
            lock.release()
        sys.exit(1)


if __name__ == "__main__":
    main()
