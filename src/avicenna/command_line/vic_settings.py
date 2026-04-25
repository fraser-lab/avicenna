import sys
import argparse
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.phil import AvicennaPHIL

parser = argparse.ArgumentParser(
    prog="vic.settings",
    description="Print the full default Avicenna PHIL parameter set.",
)
_phil_group = parser.add_mutually_exclusive_group()
_phil_group.add_argument("-c", dest="phil", action="store_true",
                         help="Print all PHIL parameters (default behaviour)")
_phil_group.add_argument("-e", dest="phil_expert_level", type=int, metavar="LEVEL",
                         help="Print PHIL parameters up to expert level LEVEL")
parser.add_argument("-a", dest="phil_attrs", action="store_true",
                    help="Show PHIL parameter attributes (use with -c or -e)")


def main():
    args = parser.parse_args()
    p = AvicennaPHIL()
    p._master.format(p._params).show(
        out=sys.stdout,
        attributes_level=2 if args.phil_attrs else 0,
        expert_level=args.phil_expert_level,
    )


if __name__ == "__main__":
    main()
