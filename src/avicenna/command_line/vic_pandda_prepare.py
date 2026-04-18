import sys
import argparse
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.pipelines.pandda_pipeline import PanDDAPreparer

parser = argparse.ArgumentParser(prog="vic.pandda_prepare")
parser.add_argument("--sources", nargs='+', type=pathlib.Path, required=True,
                    metavar="DIR",
                    help="Directories of completed qrefine jobs to concatenate")
parser.add_argument("--prefix", type=str, default="FRS",
                    help="Prefix for pandda dataset names (default: FRS)")
parser.add_argument("--offset", type=int, default=0,
                    help="Numbering offset (default: 0)")
parser.add_argument("--suffix", choices=["hoh", "nohoh"], default="hoh",
                    help="Which qrefine output to use (default: hoh)")
parser.add_argument("--reference_pdb", type=pathlib.Path,
                    help="Reference PDB for pandda.analyse")
parser.add_argument("--reference_mtz", type=pathlib.Path,
                    help="Reference MTZ for pandda.analyse")
parser.add_argument("--ground_state_range", type=str, metavar="START-END",
                    help="Dataset number range for ground state, e.g. '1-44'")
parser.add_argument("--cpus", type=int, default=32,
                    help="CPUs for pandda.analyse (default: 32)")
parser.add_argument("--min_build", type=int, default=30,
                    help="min_build_datasets (default: 30)")
parser.add_argument("--max_build", type=int, default=50,
                    help="max_build_datasets (default: 50)")
parser.add_argument("--max_new", type=int, default=1500,
                    help="max_new_datasets (default: 1500)")
parser.add_argument("--no_lr_comp_check", action="store_true",
                    help="Disable low-resolution completeness check in pandda.analyse")
parser.add_argument("--log_time_out", type=int, default=0, metavar="MINUTES",
                    help="Stop waiting for pandda log after N minutes (default: 0 = no timeout)")
parser.add_argument("--dry_run", action="store_true",
                    help="Show what would be done without copying or submitting")


def _parse_ground_state_range(prefix, range_str):
    start, _, end = range_str.partition('-')
    return [f"{prefix}_x{i:04d}" for i in range(int(start), int(end) + 1)]


def main():
    try:
        args = parser.parse_args()
        ground_state_datasets = []
        if args.ground_state_range:
            ground_state_datasets = _parse_ground_state_range(
                args.prefix, args.ground_state_range
            )
        PanDDAPreparer(
            process_dirs=list(args.sources),
            prefix=args.prefix,
            offset=args.offset,
            suffix=args.suffix,
            reference_pdb=args.reference_pdb,
            reference_mtz=args.reference_mtz,
            ground_state_datasets=ground_state_datasets,
            cpus=args.cpus,
            min_build_datasets=args.min_build,
            max_build_datasets=args.max_build,
            max_new_datasets=args.max_new,
            no_lr_comp_check=args.no_lr_comp_check,
            log_time_out=args.log_time_out,
            dry_run=args.dry_run,
        ).run()
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
