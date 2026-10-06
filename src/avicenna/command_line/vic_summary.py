import argparse
import os
import sys
import pathlib
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import (
    ProcessingScraper, ProcessingFilter,
    QRefineScraper,
    AnalysisScraper, AnalysisFilter,
)

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

parser = argparse.ArgumentParser(prog="vic.summary")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument("path", type=pathlib.Path, default=os.curdir, help="Path to folder with results, default is current directory")
parser.add_argument('-p', '--pipeline', choices=['processing', 'refine', 'analysis'], default='processing', help="Which sub-pipeline's results to summarize (default: processing)")
parser.add_argument('-V', '--verbose', action='store_true', help="Print the long-form per-dataset table instead of the short summary")
parser.add_argument('-f', '--filter', action='store_true', help="Filter results by data quality and select the best result per dataset folder before printing (requires --verbose)")
parser.add_argument('-l', '--list_write', action='store_true', help="Write a qrefine_list.txt from the (optionally filtered) results (requires --verbose)")

_SCRAPER_CLASSES = {
    "processing": (ProcessingScraper, ProcessingFilter, "processing"),
    "refine":     (QRefineScraper,    None,             None),
    "analysis":   (AnalysisScraper,   AnalysisFilter,   None),
}


def main():
    try:
        args = parser.parse_args()
        if (args.filter or args.list_write) and not args.verbose:
            parser.error("-f/--filter and -l/--list_write require --verbose")
        scraper_cls, filter_cls_factory, subdir = _SCRAPER_CLASSES[args.pipeline]
        if args.filter and filter_cls_factory is None:
            parser.error(f"-f/--filter is not available for -p {args.pipeline}")
        result_path = args.path / subdir if subdir else args.path
        scraper = scraper_cls(result_path=result_path)
        filter_cls = filter_cls_factory() if args.filter else None
        scraper.scrape_all()
        if filter_cls is not None:
            scraper.results = filter_cls.run(results=scraper.results)
        if args.verbose:
            scraper.print_info()
        else:
            scraper.print_summary()
        if args.list_write and args.pipeline == 'processing':
            scraper.write_qrefine_list()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
