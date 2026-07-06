import sys
import argparse
import pathlib
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.pipelines.analysis import AnalysisPipelineInterpreter
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


def main():
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

        # Snapshot the effective PHIL for this run (after CLI overrides); the
        # analysis_default.phil template is written only on --dry_run.
        with open(_DEFAULT_PHIL, "w") as _f:
            _f.write(phil.full_phil_string())
        print(f"Wrote {_DEFAULT_PHIL} (effective settings for this run)")

        AnalysisPipelineInterpreter(
            {"phil_string": phil.diff_phil_string()}
        ).run()
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
