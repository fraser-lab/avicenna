import sys
import pathlib
import argparse
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.software import check_programs
from avicenna.utils.phil import ProcessingPHIL, RefinePHIL, AnalysisPHIL

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

parser = argparse.ArgumentParser(
    prog="vic.check_programs",
    description="Check that all required crystallographic software is installed and report paths and versions.",
)
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument(
    "--pipeline", choices=["processing", "refine", "analysis", "all"],
    default="all",
    help="Which sub-pipeline's software requirements to check (default: all)",
)


def main():
    args = parser.parse_args()

    targets = (
        ["processing", "refine", "analysis"]
        if args.pipeline == "all"
        else [args.pipeline]
    )

    phil_map = {
        "processing": (ProcessingPHIL, lambda p: p._params.processing.programs),
        "refine":     (RefinePHIL,     lambda p: p._params.refine.programs),
        "analysis":   (AnalysisPHIL,   lambda p: p._params.analysis.programs),
    }

    any_error = False
    for name in targets:
        cls, get_programs = phil_map[name]
        print(f"\n[{name}]")
        try:
            check_programs(get_programs(cls()))
        except SystemExit:
            any_error = True

    if any_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
