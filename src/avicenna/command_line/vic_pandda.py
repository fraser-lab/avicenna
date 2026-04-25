import sys
import argparse
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.pipelines.pandda_pipeline import PanDDAPreparer
from avicenna.utils.phil import PanDDAPHIL

parser = argparse.ArgumentParser(prog="vic.pandda_prepare")
parser.add_argument("--sources", nargs='+', type=pathlib.Path,
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
                    help="Show what would be done without copying or submitting; writes avicenna.phil")
_phil_group = parser.add_mutually_exclusive_group()
_phil_group.add_argument("-c", dest="phil", action="store_true",
                         help="Print panDDA PHIL parameters and exit")
_phil_group.add_argument("-e", dest="phil_expert_level", type=int, metavar="LEVEL",
                         help="Print panDDA PHIL parameters up to expert level LEVEL and exit")
parser.add_argument("-a", dest="phil_attrs", action="store_true",
                    help="Show PHIL parameter attributes (use with -c or -e)")


def _parse_ground_state_range(range_str):
    start, _, end = range_str.partition('-')
    return [f"x{i:04d}" for i in range(int(start), int(end) + 1)]


def _build_pin_map(sources):
    pin_map = {}
    for src in sources:
        list_file = pathlib.Path(src) / "qrefine_list.txt"
        if not list_file.is_file():
            continue
        for line in list_file.read_text().splitlines():
            parts = line.strip().split(',')
            if not parts or not parts[0]:
                continue
            full_pin = parts[0]
            p = full_pin.split('_')
            if len(p) >= 3:
                pin_map[full_pin] = '_'.join(p[-3:-1])
    return pin_map


def _write_pandda_phil(args):
    p = PanDDAPHIL()
    lines = ["pandda {", "  input {"]
    if args.sources:
        sources_str = " ".join(f'"{s.resolve()}"' for s in args.sources)
        lines.append(f"    sources = {sources_str}")
    if args.reference_pdb:
        lines.append(f'    reference_pdb = "{args.reference_pdb.resolve()}"')
    if args.reference_mtz:
        lines.append(f'    reference_mtz = "{args.reference_mtz.resolve()}"')
    lines += [
        "  }",
        "  dataset {",
        f'    prefix = "{args.prefix}"',
        f"    offset = {args.offset}",
        f"    suffix = {args.suffix}",
    ]
    if args.ground_state_range:
        lines.append(f'    ground_state_range = "{args.ground_state_range}"')
    lines += [
        "  }",
        "  analyse {",
        f"    cpus = {args.cpus}",
        f"    min_build_datasets = {args.min_build}",
        f"    max_build_datasets = {args.max_build}",
        f"    max_new_datasets = {args.max_new}",
        f"    no_lr_comp_check = {args.no_lr_comp_check}",
        "  }",
        "  run {",
        f"    log_time_out = {args.log_time_out}",
        "  }",
        "}",
    ]
    p.adopt_phil_string("\n".join(lines))
    p.to_file("avicenna.phil")
    print("Written: avicenna.phil")


def main():
    try:
        args = parser.parse_args()

        if args.phil or args.phil_expert_level is not None or args.phil_attrs:
            PanDDAPHIL()._master.format(PanDDAPHIL()._params).show(
                out=sys.stdout,
                attributes_level=2 if args.phil_attrs else 0,
                expert_level=args.phil_expert_level,
            )
            sys.exit(0)

        if not args.sources:
            parser.error("--sources is required")

        ground_state_datasets = []
        if args.ground_state_range:
            ground_state_datasets = _parse_ground_state_range(args.ground_state_range)
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
            pin_map=_build_pin_map(args.sources),
        ).run()
        if args.dry_run:
            _write_pandda_phil(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
