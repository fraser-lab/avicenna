import sys
import argparse
import pathlib
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.pipelines.analysis import AnalysisPipelineInterpreter
from avicenna.pipelines.gather import reset_analysis
from avicenna.utils.info import FILENAME, ProjectInfo
from avicenna.utils.utils import (clear_stage_outputs, EXIT_FAILURE, EXIT_RUN_ACTIVE,
                                 EXIT_RUN_COMPLETE, EXIT_RUN_INCOMPLETE)
from avicenna.utils.runlock import RunLock, RunLockHeld
from avicenna.utils.phil import AnalysisPHIL
from avicenna.utils.software import check_programs

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

_DEFAULT_PHIL  = "analysis.phil"          # effective settings, written each run
_TEMPLATE_PHIL = "analysis_default.phil"  # defaults template, written by --dry_run

parser = argparse.ArgumentParser(prog="vic.analysis")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument("--settings", type=pathlib.Path, metavar="FILE",
                    help=f"PHIL settings file (default: auto-load {_DEFAULT_PHIL} if present)")
parser.add_argument("--dry_run", action="store_true",
                    help=f"Show what would be done without copying or submitting; writes {_TEMPLATE_PHIL}")
parser.add_argument("--ignore_lock", action="store_true",
                    help="Start even if another run holds the lock on the project directory")
_rerun_group = parser.add_mutually_exclusive_group()
_rerun_group.add_argument("--resume", action="store_true",
                          help="Complete an incomplete run in the output folder")
_rerun_group.add_argument("--overwrite", action="store_true",
                          help="Discard this stage's results in the output folder and start over")
_phil_group = parser.add_mutually_exclusive_group()
_phil_group.add_argument("-c", dest="phil", action="store_true",
                         help="Print analysis PHIL parameters and exit")
_phil_group.add_argument("-e", dest="phil_expert_level", type=int, metavar="LEVEL",
                         help="Print analysis PHIL parameters up to expert level LEVEL and exit")
parser.add_argument("-a", dest="phil_attrs", action="store_true",
                    help="Show PHIL parameter attributes (use with -c or -e)")

# All run parameters live in the PHIL (see `vic.analysis -c`).  Pass overrides as
# key=value tokens on the command line, e.g.:
#   vic.analysis input.input_datasets=/path/to/refine_out pandda.analyse.cpus=32


def _apply_phil_file(phil_obj, path: pathlib.Path):
    try:
        phil_obj.from_file(str(path))
        print(f"Loaded settings from {path}")
    except RuntimeError as e:
        sys.exit(f"ERROR: Invalid PHIL in {path}:\n  {e}")


# What --overwrite removes: this stage's results and logs.  Settings (.phil) are
# kept, so a restart with changed settings can happen in the same folder; other
# stages' results are not this stage's to remove.
_OVERWRITE_DIRS  = ("pandda", "pandda_logs")
_OVERWRITE_FILES = ("sentinel_analysis.py", "sentinel_analysis_submit.sh")


def _reset_analysis_records(source):
    """Take the deleted analysis results out of the analysed project's
    info.json, and remove the info.csv derived from them."""
    if not source:
        return
    derived = pathlib.Path(source) / "info.csv"
    if derived.is_file():
        derived.unlink()
        print(f"Removed {derived.name}")
    if not (pathlib.Path(source) / FILENAME).is_file():
        return
    info = ProjectInfo.load(str(source))
    reset = reset_analysis(info)
    if not reset:
        return
    try:
        info.write()
    except OSError as exc:
        print(f"WARNING: could not write {info.path}: {exc}")
        return
    print(f"Reset {len(reset)} analysed dataset(s) in {FILENAME} to refined")


def _existing_results(project_dir, resume=False, overwrite=False, source=None):
    """Deal with what an earlier analysis left in *project_dir*.

    Returns None to go ahead, or the exit status to stop with.  Stopping writes
    nothing, so the folder is left as the earlier run left it.  Resuming needs
    nothing here: rerun in place, pandda.analyse picks up what an earlier run in
    its output folder left.  *source* is the analysed project, whose info.json
    --overwrite also resets.
    """
    project_dir = pathlib.Path(project_dir)
    if overwrite:
        clear_stage_outputs(project_dir, "analysis_pipeline.log", _OVERWRITE_DIRS,
                            _OVERWRITE_FILES)
        _reset_analysis_records(source)
        return None
    pandda_dir = project_dir / "pandda"
    if not pandda_dir.is_dir() or not any(pandda_dir.iterdir()):
        return None
    if (pandda_dir / "pandda.done").is_file():
        if resume:
            print("Analysis already complete; nothing to resume.")
            return 0
        print("WARNING: Results found for all data. Either re-run with --overwrite "
              "option or create a new folder.")
        return EXIT_RUN_COMPLETE
    if not resume:
        print("WARNING: Results found for an incomplete analysis. Re-run with "
              "--overwrite option or create a new folder to start over or re-run "
              "with --resume to complete current run.")
        return EXIT_RUN_INCOMPLETE
    return None


def _project_dir(phil):
    """The project directory named by ``analysis.output.directory``."""
    return pathlib.Path(str(phil._params.analysis.output.directory or ".")).resolve()


def main():
    lock = None
    try:
        args, _extras = parser.parse_known_args()

        if args.phil or args.phil_expert_level is not None or args.phil_attrs:
            sys.stdout.write(AnalysisPHIL().full_phil_string(
                expert_level=args.phil_expert_level,
                attributes=args.phil_attrs,
            ))
            sys.exit(0)

        phil = AnalysisPHIL()
        if args.settings is not None:
            if not args.settings.exists():
                parser.error(f"Settings file not found: {args.settings}")
            _apply_phil_file(phil, args.settings)
        elif pathlib.Path(_DEFAULT_PHIL).exists():
            _apply_phil_file(phil, pathlib.Path(_DEFAULT_PHIL))

        # All run parameters are PHIL; leftover key=value tokens are PHIL overrides
        # (e.g. input.input_datasets=<dir> pandda.analyse.cpus=32).
        _flags = [e for e in _extras if e.startswith("-")]
        if _flags:
            parser.error("unrecognized arguments: " + " ".join(_flags))
        if _extras:
            try:
                phil.adopt_cli_args(_extras)
                print("Applied PHIL overrides: " + " ".join(_extras))
            except ValueError as e:
                sys.exit(f"ERROR: {e}")

        # --dry_run: just drop the template PHIL (whatever the CLI + defaults
        # populated, even nothing) and exit.  No requirements, no software check,
        # no run.
        if args.dry_run:
            with open(_TEMPLATE_PHIL, "w") as _f:
                _f.write(phil.full_phil_string())
            print(f"Written: {_TEMPLATE_PHIL}")
            return

        _steps = phil._params.analysis.steps.step
        check_programs(phil._params.analysis.programs,
                       used_programs={s.program for s in _steps},
                       program_choices=set(phil._program_blocks))

        if not phil._params.analysis.input.input_datasets:
            parser.error("analysis.input.input_datasets is required (path to the "
                         "refine output). Set it in the PHIL or pass "
                         "input.input_datasets=<dir>")

        # The project directory holds the lock and this stage's results.  The
        # lock is claimed before the check, so --overwrite cannot remove results
        # a running job is still writing.
        project_dir = _project_dir(phil)
        lock = RunLock(str(project_dir), "analysis")
        try:
            lock.acquire(ignore=args.ignore_lock)
        except RunLockHeld as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(EXIT_RUN_ACTIVE if exc.active else EXIT_FAILURE)
        stop = _existing_results(project_dir, resume=args.resume,
                                 overwrite=args.overwrite,
                                 source=phil._params.analysis.input.input_datasets)
        if stop is not None:
            lock.release()
            sys.exit(stop)

        # Snapshot the effective PHIL for this run (after CLI overrides); the
        # analysis_default.phil template is written only on --dry_run.
        with open(_DEFAULT_PHIL, "w") as _f:
            _f.write(phil.full_phil_string())
        print(f"Wrote {_DEFAULT_PHIL} (effective settings for this run)")

        failed = AnalysisPipelineInterpreter(
            {"phil_string": phil.diff_phil_string(),
             "on_submit": lock.record_jobs}
        ).run()
        # Only a run that reached nothing on the cluster still holds the lock
        # here; otherwise the completion sentinel releases it.
        lock.release()
        if failed:
            sys.exit(EXIT_FAILURE)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        # Before submission nothing else will release the lock; after it, the
        # cluster job holds the directory and release() leaves it alone.
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()
