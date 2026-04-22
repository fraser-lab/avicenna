import os
import sys
import time
import argparse
import pathlib
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import ProcessingWatcher, QRefineWatcher, PanDDAWatcher, QRefineWriter, ProgressBar

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

parser = argparse.ArgumentParser(prog="proc_watcher")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument("procpath", type=pathlib.Path, default=os.curdir, help="Path to processing folder, default is current directory")
parser.add_argument("-p", "--pincode", type=str, default=None, help="Pin code to watch for; if not provided, will watch for all pin codes in the folder")
parser.add_argument("--pipeline", default="processing", help="Which pipeline to watch (default: processing)")
_mode = parser.add_mutually_exclusive_group()
_mode.add_argument("-d", "--details", action="store_true", default=False, help="Show detailed status for each pin code")
_mode.add_argument("-l", "--log", action="store_true", default=False, help="Print omnibus log to stdout as jobs run (no progress bar)")
_mode.add_argument("-m", "--multiline", action="store_true", default=False, help="Show per-category progress bars, one line each (default)")
_mode.add_argument("-s", "--simple", action="store_true", default=False, help="Show compact single-line progress bar")


def _append_qrefine_paths(procpath):
    writer = QRefineWriter(procpath)
    list_path   = writer.process_dir / 'qrefine_list.txt'
    params_path = writer.process_dir / 'qrefine_input_parameters.txt'
    omnibus = writer.process_dir / 'processing.log'
    lines = []
    if list_path.is_file():
        lines.append(f"qrefine_list.txt:             {list_path}\n")
    if params_path.is_file():
        lines.append(f"qrefine_input_parameters.txt: {params_path}\n")
    if lines and omnibus.is_file():
        with open(omnibus, 'a') as fh:
            for line in lines:
                fh.write(line)
    for line in lines:
        print(line, end="")


_WATCHER_CLASSES = {
    "processing" : (ProcessingWatcher, "processing"),
    "qrefine"    : (QRefineWatcher,    None),
    "pandda"     : (PanDDAWatcher,     None),
}


def main():
    try:
        args = parser.parse_args()
        if args.pipeline not in _WATCHER_CLASSES:
            known = ", ".join(sorted(_WATCHER_CLASSES))
            parser.error(f"unknown pipeline '{args.pipeline}'; known pipelines: {known}")
        watcher_cls, subdir = _WATCHER_CLASSES[args.pipeline]
        procpath = args.procpath / subdir if subdir else args.procpath
        watcher = watcher_cls(procpath=procpath, pincode=args.pincode)

        if args.log:
            while True:
                watcher.tail_log()
                if not watcher.is_running():
                    break
                time.sleep(1)
            watcher.tail_log()
            print()
            _append_qrefine_paths(args.procpath)

        elif args.details:
            while True:
                watcher.show_details()
                if not watcher.is_running():
                    print()
                    _append_qrefine_paths(args.procpath)
                    break
                time.sleep(1)

        else:
            bar = ProgressBar(watcher, multiline=not args.simple)
            while True:
                still_running = bar.draw()
                if not still_running:
                    print()
                    _append_qrefine_paths(args.procpath)
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nWatcher stopped. Jobs are still running on the cluster.")
        sys.exit(1)


if __name__ == "__main__":
    main()
