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
from avicenna.pipelines.qrefine_pipeline import RunQRefine

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

parser = argparse.ArgumentParser(prog="vic.refine")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
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
                    help="Scan processing output and write qrefine_list.txt and qrefine_input_parameters.txt without submitting jobs")
parser.add_argument("--silent", action="store_true",
                    help="Submit jobs without tailing the log")


def main():
    args = parser.parse_args()

    if args.dry_run:
        QRefineWriter(args.output).write()
        return

    RunQRefine(
        process_dir=args.output,
        dataset_list=args.list,
        params_file=args.params,
        dry_run=False,
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


if __name__ == "__main__":
    main()
