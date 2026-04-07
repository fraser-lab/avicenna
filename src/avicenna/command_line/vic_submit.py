import os
import sys
import argparse
import pathlib
import subprocess
import time
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import PinCollector, ParamWriter, Watcher
from avicenna.pipelines.proc_pipeline import RunProcess

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

parser = argparse.ArgumentParser(prog="xds_starter")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument("-d", "--data", type=pathlib.Path, nargs="+", help="Path(s) to data; each should have the format <synchrotron>_<date_YYYYMMDD>_transfer")
parser.add_argument("-s", "--scripts", type=pathlib.Path, default="../scripts", help="Path to all the scripts (default is ../scripts)")
parser.add_argument("-p", "--pinlist", type=str, help="Filename for pin list file, typically pin_list.txt")
parser.add_argument("-x", "--xdsparams", type=str, help="Filename for XDS param file, usually xds_input_parameters.txt")
parser.add_argument("-e", "--edge", type=float, default=0.8, help="Maximum resolution (default=0.8)")
parser.add_argument("-i", "--idxrange", type=str, default="first_half", help="Indexing range; currently can only do 'first_half/all/last_half/quarters/eights' (default: 'first_half')")
parser.add_argument("-n", "--ncpu", type=int, default=8, help="Number of CPUs (default=8)")
parser.add_argument("--altsym", choices=['false', 'top', 'all'], default='all', help="Whether to try to index in alternate space groups")
parser.add_argument("--scaling", choices=['correct', 'aimless'], default='correct', help="Which scaling program will be used")
parser.add_argument("--recycle", action='store_true', help="Toggle whether parameters in XPARM.XDS are recycled")
parser.add_argument("--refcell", choices=['true', 'false', 'both'], default='both', help="Whether unit cell is refined during INTEGRATE step")
parser.add_argument("--reslim", type=str, default='cc12', help="Set resolution limit or the way it's calculated (don't change for now)")
parser.add_argument("--cleanup", type=str, default='partial', help="I'm not sure what this is...")
parser.add_argument("--refhkl", type=pathlib.Path, default=None, help="Path to reference HKL file")
parser.add_argument("--refmtz", type=pathlib.Path, default=None, help="Path to reference MTZ file")
parser.add_argument("--python", action="store_true", help="Run the Python-based processing pipeline (proc_pipeline.py) instead of the bash scripts")
parser.add_argument("-o", "--output", type=pathlib.Path, default=pathlib.Path(os.curdir), help="Path to output folder (default: current directory)")
parser.add_argument("--watch", action="store_true", help="Whether to watch the processing progress after submission")


if __name__ == "__main__":
    args = parser.parse_args()
    params = vars(args)

    # Resolve pin list: read from file or collect via PinCollector
    if args.pinlist and os.path.isfile(args.pinlist):
        with open(args.pinlist) as fh:
            params["pins"] = [line.strip() for line in fh if line.strip()]
    else:
        pincollector = PinCollector(paths=args.data)
        pincollector.collect_pins()
        params["pins"] = pincollector.pin_list

    # Resolve XDS params: read from file and merge into params
    if args.xdsparams and os.path.isfile(args.xdsparams):
        with open(args.xdsparams) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                params[key.strip()] = value.strip()
        with open(args.xdsparams, "w") as fh:
            for key, value in params.items():
                fh.write(f"{key}={value}\n")
    else:
        args.data = args.data[0]
        paramwriter = ParamWriter(args=args)
        paramwriter.build_parameters()
        with open("xds_input_parameters.txt") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                params[key.strip()] = value.strip()

    if args.python:
        proc = RunProcess(params)
        proc.run()
    else:
        # run bash script
        if (os.path.isfile('./pin_list.txt') and os.path.isfile('./xds_input_parameters.txt')):
            script_path = os.path.join(os.path.abspath(args.scripts), 'xds_pipeline_prep.sh')
            subprocess.run(script_path)
        else:
            print("ERROR: pin_list.txt and/or xds_input_parameters.txt not found!")
            exit()

    if args.watch:
        watcher = Watcher(procpath=os.path.abspath(args.output))
        while True:
            time.sleep(1)
            watcher.show_progress()
