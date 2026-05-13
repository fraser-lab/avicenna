import contextlib
import io
import os
import sys
import argparse
import pathlib
import time
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import QRefineWatcher, QRefineScraper, QRefineWriter
from avicenna.pipelines.legacy.qrefine_pipeline import RunQRefine
from avicenna.utils.phil import RefinePHIL, AvicennaPHIL
from avicenna.utils.software import check_programs

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

_DEFAULT_PHIL = "refine.phil"

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
                    help=f"Scan processing output and write qrefine_list.txt, qrefine_input_parameters.txt, and {_DEFAULT_PHIL} without submitting jobs")
parser.add_argument("--legacy", action="store_true",
                    help="Use the hard-coded legacy pipeline instead of the PHIL-driven interpreter")
parser.add_argument("--silent", action="store_true",
                    help="Submit jobs without tailing the log")
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


def main():
    try:
        args = parser.parse_args()

        if args.phil or args.phil_expert_level is not None or args.phil_attrs:
            av = AvicennaPHIL()
            av._master.format(av._params).show(
                out=sys.stdout,
                attributes_level=2 if args.phil_attrs else 0,
                expert_level=args.phil_expert_level,
            )
            sys.exit(0)

        phil = RefinePHIL()
        if args.settings is not None:
            if not args.settings.exists():
                parser.error(f"Settings file not found: {args.settings}")
            _apply_phil_file(phil, args.settings)
        elif pathlib.Path(_DEFAULT_PHIL).exists():
            _apply_phil_file(phil, pathlib.Path(_DEFAULT_PHIL))

        check_programs(phil._params.refine.programs)

        if args.dry_run:
            QRefineWriter(args.output).write()
            phil.adopt_phil_string(f"""\
refine {{
  input {{
    output_dir = "{pathlib.Path(args.output).resolve()}"
    dataset_list = "{args.list}"
    params_file = "{args.params}"
  }}
}}""")
            with open(_DEFAULT_PHIL, "w") as _f:
                _f.write(phil.full_phil_string())
            print(f"Written: {_DEFAULT_PHIL}")
            return

        if args.legacy:
            RunQRefine(
                process_dir=args.output,
                dataset_list=args.list,
                params_file=args.params,
                dry_run=False,
                fill=not args.no_fill,
            ).run()
        else:
            RunQRefine(                # step #3: swap to RefinementPipelineInterpreter
                process_dir=args.output,
                dataset_list=args.list,
                params_file=args.params,
                dry_run=False,
                fill=not args.no_fill,
            ).run()

        if args.silent:
            return

        procpath = os.path.abspath(args.output)
        omnibus = os.path.join(procpath, "qrefine.log")
        watcher = QRefineWatcher(procpath=procpath)
        while True:
            watcher.tail_logs(log_file=omnibus)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                still_running = watcher.show_progress()
            if not still_running:
                break
            time.sleep(1)
        watcher.tail_logs(log_file=omnibus)
        print()
        watcher.print_summary(omnibus=omnibus)
        scraper = QRefineScraper(procpath)
        scraper.scrape_all()
        scraper.print_info()
    except KeyboardInterrupt:
        print("\nInterrupted. Any submitted jobs are still running on the cluster.")
        sys.exit(1)


if __name__ == "__main__":
    main()
