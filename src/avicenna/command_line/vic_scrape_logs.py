import argparse
import os
import sys
import pathlib
from importlib.metadata import version, PackageNotFoundError
# import matplotlib.pyplot as plt
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import Scraper

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

parser = argparse.ArgumentParser(prog="xds_scraper")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument("path", type=pathlib.Path, default=os.curdir, help="Path to folder with results, default is current directory")
parser.add_argument('-p', '--plot', action='store_true', help="Whether to plot unit cell parameters (a,b,c,alpha,beta,gamma) from the results")


if __name__ == "__main__":
    args = parser.parse_args()
    scraper = Scraper(result_path=args.path)
    scraper.scrape_all()
    scraper.print_info()
    if args.plot:
        scraper.plot_unit_cells()
