import argparse
import os
import sys
import pathlib
from importlib.metadata import version, PackageNotFoundError
# import matplotlib.pyplot as plt
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import ProcessingScraper, ProcessingFilter, QRefineScraper, RefinementFilter

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

parser = argparse.ArgumentParser(prog="xds_scraper")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument("path", type=pathlib.Path, default=os.curdir, help="Path to folder with results, default is current directory")
parser.add_argument('-p', '--plot',   action='store_true', help="Whether to plot unit cell parameters (a,b,c,alpha,beta,gamma) from the results")
parser.add_argument('-f', '--filter',     action='store_true', help="Filter results by data quality and select the best result per dataset folder before printing")
parser.add_argument('-l', '--list_write', action='store_true', help="Write a qrefine_list.txt from the (optionally filtered) results")
parser.add_argument('--pipeline', choices=['processing', 'qrefine'], default='processing', help="Whether to write the qrefine_list.txt for the processing pipeline (default) or the qrefine pipeline")


def main():
    try:
        args = parser.parse_args()
        if args.pipeline == 'processing':
            scraper = ProcessingScraper(result_path=args.path)
            filter_cls = ProcessingFilter() if args.filter else None
        elif args.pipeline == 'qrefine':
            scraper = QRefineScraper(result_path=args.path)
            filter_cls = RefinementFilter() if args.filter else None
        scraper.scrape_all()
        if filter_cls is not None:
            scraper.results = filter_cls.run(results=scraper.results)
        scraper.print_info()
        if args.list_write and args.pipeline == 'processing':
            scraper.write_qrefine_list()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
