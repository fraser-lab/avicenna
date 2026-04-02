import argparse
import os
import sys
import matplotlib.pyplot as plt
import pathlib
from avicenna.utils.utils import Scraper

parser = argparse.ArgumentParser(prog="xds_starter")
parser.add_argument("path", type=pathlib.Path, default=os.curdir, help="Path to folder with results, default is current directory")
parser.add_argument('-p', '--plot', action='store_true', help="Whether to plot unit cell parameters (a,b,c,alpha,beta,gamma) from the results")


if __name__ == "__main__":
    args = parser.parse_args()
    scraper = Scraper(result_path=args.path)
    scraper.scrape_all()
    scraper.print_info()
    if args.plot:
        scraper.plot_unit_cells()
