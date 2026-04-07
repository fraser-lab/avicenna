import os
import sys
import time
import argparse
import pathlib
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import Watcher

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

parser = argparse.ArgumentParser(prog="proc_watcher")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument("procpath", type=pathlib.Path, default=os.curdir, help="Path to processing folder, default is current directory")
parser.add_argument("-p", "--pincode", type=str, default=None, help="Pin code to watch for; if not provided, will watch for all pin codes in the folder")
parser.add_argument("-d", "--details", action="store_true", default=False, help="Show detailed status for each pin code; if not provided, show summary counts only")


if __name__ == "__main__":
    args = parser.parse_args()
    watcher = Watcher(procpath=args.procpath, pincode=args.pincode, details=args.details)
    while True:
        time.sleep(1)
        if not watcher.run():
            print()
            break