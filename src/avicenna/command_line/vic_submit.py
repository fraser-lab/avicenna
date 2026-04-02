import os
import sys
import argparse
import pathlib
import subprocess
from avicenna.utils.utils import PinCollector, ParamWriter

parser = argparse.ArgumentParser(prog="xds_starter")
parser.add_argument("-d", "--data", type=pathlib.Path, help="Path to data; should have the format <synchrotron>_<date_YYYYMMDD>_transfer")
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
 

if __name__ == "__main__":
    args = parser.parse_args()
    
    if args.pinlist is None:
        pincollector = PinCollector(dst=str(args.data))
        pincollector.collect_pins()
    if args.xdsparams is None:
        paramwriter = ParamWriter(args=args)
        paramwriter.build_parameters()

    # run script
    if (os.path.isfile('./pin_list.txt') and os.path.isfile('./xds_input_parameters.txt')):
        script_path = os.path.join(os.path.abspath(args.scripts), 'xds_pipeline_prep.sh')
        subprocess.run(script_path)
    else:
        print ("ERROR: pin_list.txt and/or xds_input_parameters.txt not found!")
        exit()
