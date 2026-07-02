import contextlib
import io
import os
import sys
import argparse
import pathlib
import time
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import QRefineWatcher, QRefineScraper, QRefineWriter, ProgressBar, log_phil_diff, wait_for_sentinel
from avicenna.pipelines.legacy.qrefine_pipeline import RunQRefine
from avicenna.pipelines.refinement import RefinementOrchestrator
from avicenna.pipelines.sentinel import RefinementSentinel
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
parser.add_argument("-l", "--list", type=pathlib.Path,
                    default=pathlib.Path("qrefine_list.txt"),
                    help="Dataset list file (default: qrefine_list.txt)")
parser.add_argument("-p", "--params", type=pathlib.Path,
                    default=pathlib.Path("qrefine_input_parameters.txt"),
                    help="MR parameter file (default: qrefine_input_parameters.txt)")
parser.add_argument("--dry_run", action="store_true",
                    help=f"Scan processing output and write qrefine_list.txt, qrefine_input_parameters.txt, and {_TEMPLATE_PHIL} without submitting jobs")
parser.add_argument("--legacy", action="store_true",
                    help="Use the hard-coded legacy pipeline instead of the PHIL-driven interpreter")
parser.add_argument("--silent", action="store_true",
                    help="Submit jobs without tailing the log")
parser.add_argument("--no-prog-log", dest="no_prog_log", action="store_true",
                    help="Disable the multiline progress bar; show plain progress updates instead")
parser.add_argument("--no_fill", action="store_true",
                    help="Skip populating missing/invalid reflections in the final MTZ (not recommended for panDDA input)")
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
    dataset_list = "{args.list}"
    params_file = "{args.params}"
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

        sentinel_submitted = False
        if args.legacy:
            RunQRefine(
                process_dir=args.output,
                dataset_list=args.list,
                params_file=args.params,
                dry_run=False,
                fill=not args.no_fill,
            ).run()
        else:
            if args.no_fill:
                # Disable any fill step in the PHIL so the interpreter skips it
                # (the legacy `fill` constructor flag has no effect on this path).
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
                params_file=args.params,
                dry_run=False,
                fill=not args.no_fill,
                phil_string=phil.diff_phil_string(),
            ).run()
            # Finalization (scrape + analysis.phil) runs on job completion via an
            # SGE sentinel, so it survives --silent / a dropped connection.
            if main_job_id:
                RefinementSentinel(os.path.abspath(args.output)).submit(main_job_id)
                sentinel_submitted = True

        if args.silent:
            return

        procpath = os.path.abspath(args.output)
        omnibus = os.path.join(procpath, "qrefine.log")
        watcher = QRefineWatcher(procpath=procpath)
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
        if sentinel_submitted:
            if not wait_for_sentinel(omnibus):
                print("Sentinel marker not seen within the timeout; it will still "
                      "finalize on the cluster (analysis.phil).")
        watcher.print_summary(omnibus=omnibus)
        # Short-form summary to the main log; the full table is a deep-analysis
        # tool available via `vic.summary -p refine -V`.
        scraper = QRefineScraper(procpath)
        scraper.scrape_all()
        scraper.print_summary(omnibus=omnibus)
    except KeyboardInterrupt:
        print("\nInterrupted. Any submitted jobs are still running on the cluster.")
        sys.exit(1)


if __name__ == "__main__":
    main()
