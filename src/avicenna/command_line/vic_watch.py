import os
import sys
import time
import argparse
import pathlib
from avicenna.utils.utils import Watcher

parser = argparse.ArgumentParser(prog="proc_watcher")
parser.add_argument("procpath", type=pathlib.Path, default=os.curdir, help="Path to processing folder, default is current directory")
parser.add_argument("-p", "--pincode", type=str, default=None, help="Pin code to watch for; if not provided, will watch for all pin codes in the folder")
parser.add_argument("-d", "--details", action="store_true", default=False, help="Show detailed status for each pin code; if not provided, show summary counts only")


if __name__ == "__main__":
    args = parser.parse_args()
    watcher = Watcher(args=args)
    while True:
        time.sleep(1)
        watcher.run()