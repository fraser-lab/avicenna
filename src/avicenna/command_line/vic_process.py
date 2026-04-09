import contextlib
import io
import os
import sys
import argparse
import pathlib
import subprocess
import time
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import DataFinder, ParamWriter, Watcher, Scraper
from avicenna.pipelines.proc_pipeline import RunProcess

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

parser = argparse.ArgumentParser(prog="xds_starter")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument("-d", "--data", type=pathlib.Path, nargs="+", help="Path(s) to data; each should have the format <synchrotron>_<date_YYYYMMDD>_transfer")
parser.add_argument("-p", "--pinlist", type=str, help="Filename for pin list file, typically pin_list.txt")
parser.add_argument("-x", "--xdsparams", type=str, help="Filename for XDS param file, usually xds_input_parameters.txt")
parser.add_argument("--bash", action="store_true", help="Run the bash scripts instead of the Python-based processing pipeline")
parser.add_argument("-o", "--output", type=pathlib.Path, default=pathlib.Path(os.curdir), help="Path to output folder (default: current directory)")
_mode = parser.add_mutually_exclusive_group()
_mode.add_argument("--watch", action="store_true", help="Watch processing progress after submission (progress bar)")
_mode.add_argument("-l", "--print_log", action="store_true", help="Print omnibus processing.log to stdout as jobs run (no progress bar)")
parser.add_argument("--dry_run", action="store_true", help="Create pin_list.txt and xds_input_parameters.txt without submitting jobs")


XDS_PARAM_DEFAULTS = {
    "script_dir":            "../scripts",
    "detector_edge":         "0.8",
    "indexing_images":       "first_half",
    "try_alt_sym":           "all",
    "ncpu":                  "8",
    "no_symmetry":           "false",
    "scaling":               "correct",
    "recycle_xds":           "false",
    "refine_cell_integrate": "both",
    "res_limit":             "cc12",
    "cleanup":               "partial",
    "ref_hkl":               "none",
    "ref_mtz":               "none",
}

def _read_xds_params(filepath):
    """Read XDS params from a params file, falling back to defaults for missing keys."""
    result = dict(XDS_PARAM_DEFAULTS)
    with open(filepath) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in XDS_PARAM_DEFAULTS:
                result[key] = value.strip()
    return result


def main():
    args = parser.parse_args()
    if not args.pinlist and not args.data:
        parser.error("either -d/--data or -p/--pinlist must be provided")

    if args.dry_run:
        if args.pinlist or args.xdsparams:
            parser.error("--dry_run is incompatible with --pinlist and --xdsparams")
        if not args.data:
            parser.error("--dry_run requires -d/--data")
        for fname in ("pin_list.txt", "xds_input_parameters.txt"):
            if os.path.isfile(fname):
                os.rename(fname, fname + ".bak")
                print(f"Backed up {fname} -> {fname}.bak")
        _, _, data_path = DataFinder(paths=args.data).collect()
        args.data = data_path
        ParamWriter(args=args).build_parameters()
        print("Dry run complete.")
        sys.exit(0)

    params = {}
    data_path = None

    # Phase 1: Resolve pin list
    if args.pinlist and os.path.isfile(args.pinlist):
        with open(args.pinlist) as fh:
            lines = [line.strip() for line in fh if line.strip()]
        # Detect deprecated format (relative paths)
        if lines and not os.path.isabs(lines[0].split(",")[0]):
            if not args.data:
                sys.exit(
                    "ERROR: pin_list.txt uses deprecated relative paths. "
                    "Provide -d/--data to reconstruct absolute paths."
                )
            data_path = DataFinder(paths=args.data).paths[0]
            image_dir = os.path.dirname(str(data_path))
            lines = [
                f"{os.path.join(image_dir, l.split(',')[0])},{','.join(l.split(',')[1:])}"
                for l in lines
            ]
            with open(args.pinlist, "w") as fh:
                fh.write("\n".join(lines) + "\n")
        parents = {os.path.dirname(l.split(",")[0]) for l in lines}
        if len(parents) > 1:
            sys.exit(
                "ERROR: pin_list.txt entries have different parent directories:\n" +
                "\n".join(f"  {p}" for p in sorted(parents))
            )
        params["image_dir"] = parents.pop()
        params["pins"] = lines
    else:
        pin_list, image_dir, data_path = DataFinder(paths=args.data).collect()
        params["pins"] = pin_list
        params["image_dir"] = image_dir

    # Phase 2: Resolve XDS params (file overrides args)
    if args.xdsparams and os.path.isfile(args.xdsparams):
        params.update(_read_xds_params(args.xdsparams))
    else:
        if data_path is None:
            sys.exit(
                "ERROR: -d/--data is required when using a --pinlist but not using --xdsparams"
            )
        args.data = data_path
        ParamWriter(args=args).build_parameters()
        params.update(_read_xds_params("xds_input_parameters.txt"))

    if args.bash:        # run Galen's original bash scripts for XDS processing
        if (os.path.isfile('./pin_list.txt') and os.path.isfile('./xds_input_parameters.txt')):
            script_path = os.path.join(os.path.abspath(params["script_dir"]), 'xds_pipeline_prep.sh')
            subprocess.run(script_path)
        else:
            print("ERROR: pin_list.txt and/or xds_input_parameters.txt not found!")
            exit()
    else:
        proc = RunProcess(params)
        proc.run()

    if args.watch:
        watcher = Watcher(procpath=os.path.abspath(args.output))
        while watcher.show_progress():
            time.sleep(1)
        print()
        watcher.print_summary()
        scraper = Scraper(str(os.path.abspath(args.output)))
        scraper.scrape_all()
        scraper.print_info()

    elif args.print_log:
        omnibus = os.path.join(os.path.abspath(args.output), "processing.log")
        watcher = Watcher(procpath=os.path.abspath(args.output))
        log_offset = 0
        while True:
            if os.path.isfile(omnibus):
                with open(omnibus) as fh:
                    lines = fh.readlines()
                for line in lines[log_offset:]:
                    print(line, end="", flush=True)
                log_offset = len(lines)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                still_running = watcher.show_progress()
            if not still_running:
                break
            time.sleep(1)
        # flush any remaining log lines
        if os.path.isfile(omnibus):
            with open(omnibus) as fh:
                lines = fh.readlines()
            for line in lines[log_offset:]:
                print(line, end="", flush=True)
        print()
        watcher.print_summary(omnibus=omnibus)
        scraper = Scraper(str(os.path.abspath(args.output)))
        scraper.scrape_all()
        scraper.print_info()


if __name__ == "__main__":
    main()
