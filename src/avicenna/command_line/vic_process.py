import contextlib
import io
import os
import re
import sys
import argparse
import pathlib
import subprocess
import time
from importlib.metadata import version, PackageNotFoundError
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import DataFinder, ParamWriter, Watcher, ProcessingScraper, ProgressBar, log_phil_diff, wait_for_sentinel
from avicenna.pipelines.legacy.proc_pipeline import RunProcess
from avicenna.pipelines.processing import ProcessingOrchestrator
from avicenna.pipelines.sentinel import ProcessingSentinel
from avicenna.utils.phil import ProcessingPHIL
from avicenna.utils.software import check_programs

try:
    _version = version("avicenna")
except PackageNotFoundError:
    _version = "unknown"

_DEFAULT_PHIL  = "processing.phil"          # effective settings, written each run
_TEMPLATE_PHIL = "processing_default.phil"  # defaults template, written by --dry_run

parser = argparse.ArgumentParser(prog="vic.process")
parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {_version}")
parser.add_argument("--settings", type=pathlib.Path, metavar="FILE",
                    help=f"PHIL settings file (default: auto-load {_DEFAULT_PHIL} if present)")
parser.add_argument("-d", "--data", type=pathlib.Path, nargs="+", help="Path(s) to data; each should have the format <synchrotron>_<date_YYYYMMDD>_transfer")
parser.add_argument("-p", "--pinlist", type=str, help="Filename for pin list file, typically pin_list.txt")
parser.add_argument("-x", "--xdsparams", type=str, help="Filename for XDS param file, usually xds_input_parameters.txt")
parser.add_argument("--bash", action="store_true", help="Run the bash scripts instead of the Python-based processing pipeline")
parser.add_argument("--legacy", action="store_true",
                    help="Use the hard-coded legacy pipeline instead of the PHIL-driven interpreter")
parser.add_argument("-o", "--output", type=pathlib.Path, default=pathlib.Path(os.curdir), help="Path to output folder (default: current directory)")
parser.add_argument("--dry_run", action="store_true", help=f"Create pin_list.txt and {_TEMPLATE_PHIL} without submitting jobs")
parser.add_argument("--silent",    action="store_true", help="Submit jobs without tailing the log")
parser.add_argument("--no-prog-log", dest="no_prog_log", action="store_true", help="Disable the multiline progress bar; show plain progress updates instead")
parser.add_argument("--no_filter", action="store_true", help="Skip quality filter when writing qrefine_list.txt; write all completed results")
parser.add_argument("--xds_root", type=pathlib.Path, help="Path to the XDS install folder, default is an SBGrid install (/programs/<OS>/xds)")
_phil_group = parser.add_mutually_exclusive_group()
_phil_group.add_argument("-c", dest="phil", action="store_true",
                         help="Print processing PHIL parameters and exit")
_phil_group.add_argument("-e", dest="phil_expert_level", type=int, metavar="LEVEL",
                         help="Print processing PHIL parameters up to expert level LEVEL and exit")
parser.add_argument("-a", dest="phil_attrs", action="store_true",
                    help="Show PHIL parameter attributes (use with -c or -e)")


def _apply_phil_file(phil_obj, path: pathlib.Path):
    try:
        phil_obj.from_file(str(path))
        print(f"Loaded settings from {path}")
    except RuntimeError as e:
        sys.exit(f"ERROR: Invalid PHIL in {path}:\n  {e}")


XDS_PARAM_DEFAULTS = {
    "script_dir":            "../scripts",
    "detector_edge":         "0.8",
    "indexing_images":       "first_half",
    "on_indexing_failure":   "auto",
    "retry_mode":            "auto",
    "on_integration_failure": "reject",
    "ncpu":                  "auto",
    "no_symmetry":           "false",
    "scaling":               "correct",
    "recycle_xds":           "false",
    "refine_cell_integrate": "true",
    "res_limit":             "cc12",
    "cleanup":               "partial",
    "ref_hkl":               "none",
    "ref_mtz":               "none",
}

class _Tee:
    """Write to both the underlying stream and a log file simultaneously."""
    def __init__(self, stream, path):
        self._stream = stream
        self._path   = path
    def write(self, data):
        self._stream.write(data)
        with open(self._path, 'a') as f:
            f.write(data)
    def flush(self):
        self._stream.flush()


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


def _params_from_phil(phil):
    """Flatten the processing PHIL into the flat XDS-param keys the executor reads.

    PHIL is the single source on the default path (no xds_input_parameters.txt).
    Starts from XDS_PARAM_DEFAULTS so dropped/dead keys (script_dir, cleanup,
    recycle_xds) keep safe defaults, and the per-step failure params (read from
    the step scope by the interpreter) keep harmless placeholders.
    """
    p = phil._params.processing
    out = dict(XDS_PARAM_DEFAULTS)
    out["ncpu"]                  = str(p.xds.ncpu)
    out["detector_edge"]         = str(p.xds.detector_edge)
    out["res_limit"]             = p.xds.res_limit
    out["scaling"]               = p.xds.scaling
    out["indexing_images"]       = p.dataset.indexing_images
    out["refine_cell_integrate"] = p.dataset.refine_cell_integrate
    out["no_symmetry"]           = "true" if p.dataset.no_symmetry else "false"
    out["ref_hkl"]               = "none" if p.dataset.reference_hkl is None else str(p.dataset.reference_hkl)
    out["ref_mtz"]               = "none" if p.input.reference_mtz is None else str(p.input.reference_mtz)
    return out


def _watch_with_bar(watcher, omnibus):
    """Tail per-dataset pipeline logs with a compact progress bar pinned below.

    Captures new log lines to a buffer; only erases the bar (which causes a
    visible blink) when there is content to splice in above it.  Otherwise the
    bar's draw() does a spinner-only in-place update.  The braille spinner in
    the title line animates each tick so a frozen terminal is visible.
    """
    bar = ProgressBar(watcher, multiline=True, separator=True)
    while True:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            watcher.tail_logs(log_file=omnibus)
        new_log = buf.getvalue()
        if new_log:
            bar.erase()
            sys.stdout.write(new_log)
            sys.stdout.flush()
        still_running = bar.draw()
        if not still_running:
            break
        time.sleep(1)


def _auto_enable_known_symmetry(phil, extras):
    """Auto-set ``known_symmetry.enabled`` when its data fields are supplied as
    direct CLI ``key=value`` tokens.

    Typing a space group / unit cell / reference HKL on the command line is itself
    the opt-in, so the explicit ``enabled`` toggle is only required for hand-edited
    PHIL files (and ``.phil`` files passed on the CLI, which are treated like
    files and excluded here).  An explicit CLI ``enabled=...`` token is respected
    -- explicit beats inferred.  Returns True if the toggle was flipped on.
    """
    assign = [e for e in extras if "=" in e and not e.endswith(".phil")]
    if not assign:
        return False
    data_set = enabled_explicit = False
    try:
        interp = phil._master.command_line_argument_interpreter(home_scope="processing")
        for obj in interp.process_args(assign):
            path = obj.as_str().split("=", 1)[0].strip()
            leaf = path.rsplit(".", 1)[-1]
            if "known_symmetry" in path:
                if leaf in ("space_group", "unit_cell", "reference_hkl"):
                    data_set = True
                elif leaf == "enabled":
                    enabled_explicit = True
    except Exception:
        return False
    if data_set and not enabled_explicit and not phil._params.processing.known_symmetry.enabled:
        phil.adopt_phil_string(
            "processing {\n  known_symmetry {\n    enabled = True\n  }\n}\n")
        return True
    return False


def main():
    try:
        args, _extras = parser.parse_known_args()

        # -d/--data is greedy (nargs="+"), so PHIL overrides placed AFTER -d get
        # swallowed into args.data instead of left as extras.  Re-route any token
        # that is a PHIL assignment (key=value) or a .phil file back to the
        # override path so argument order does not matter.
        if args.data:
            _kept, _routed = [], []
            for _d in args.data:
                _s = str(_d)
                if re.match(r"^[A-Za-z_]\w*(\.\w+)*=", _s) or (_s.endswith(".phil") and os.path.isfile(_s)):
                    _routed.append(_s)
                else:
                    _kept.append(_d)
            args.data = _kept or None
            _extras = _routed + _extras

        if args.phil or args.phil_expert_level is not None or args.phil_attrs:
            sys.stdout.write(ProcessingPHIL().full_phil_string(
                expert_level=args.phil_expert_level,
                attributes=args.phil_attrs,
            ))
            sys.exit(0)

        phil = ProcessingPHIL()
        if args.settings is not None:
            if not args.settings.exists():
                parser.error(f"Settings file not found: {args.settings}")
            _apply_phil_file(phil, args.settings)
        elif pathlib.Path(_DEFAULT_PHIL).exists():
            _apply_phil_file(phil, pathlib.Path(_DEFAULT_PHIL))

        # PHIL overrides given as CLI args (e.g. processing.xds.ncpu=16 res_limit=i_over_sigma).
        _flags = [e for e in _extras if e.startswith("-")]
        if _flags:
            parser.error("unrecognized arguments: " + " ".join(_flags))
        if _extras:
            try:
                phil.adopt_cli_args(_extras)
                print("Applied PHIL overrides: " + " ".join(_extras))
            except ValueError as e:
                sys.exit(f"ERROR: {e}")
            if _auto_enable_known_symmetry(phil, _extras):
                print("known_symmetry data supplied on CLI -> known_symmetry.enabled = True")

        check_programs(phil._params.processing.programs)

        # PHIL input/output values take precedence; CLI args are the fallback.
        _pi = phil._params.processing.input
        _po = phil._params.processing.output
        if _pi.data:
            _d = _pi.data
            args.data = [pathlib.Path(x) for x in (_d if isinstance(_d, (list, tuple)) else [_d])]
        if _pi.pinlist:
            args.pinlist = _pi.pinlist
        if _po.directory and _po.directory != ".":
            args.output = pathlib.Path(_po.directory)

        if not args.pinlist and not args.data:
            parser.error("either -d/--data or -p/--pinlist must be provided")

        if args.dry_run:
            if args.pinlist or args.xdsparams:
                parser.error("--dry_run is incompatible with --pinlist and --xdsparams")
            if not args.data:
                parser.error("--dry_run requires -d/--data")
            for fname in ("pin_list.txt", _TEMPLATE_PHIL):
                if os.path.isfile(fname):
                    os.rename(fname, fname + ".bak")
                    print(f"Backed up {fname} -> {fname}.bak")
            _, _, data_path = DataFinder(paths=args.data).collect()
            data_str = " ".join(f'"{d}"' for d in args.data)
            output_str = str(pathlib.Path(args.output).resolve())
            phil.adopt_phil_string(f"""\
processing {{
  input {{
    data = {data_str}
    pinlist = "pin_list.txt"
  }}
  output {{
    directory = "{output_str}"
  }}
}}""")
            with open(_TEMPLATE_PHIL, "w") as _f:
                _f.write(phil.full_phil_string())
            print(f"Written: {_TEMPLATE_PHIL}")
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

        # Phase 2: Resolve XDS / dataset settings.
        #  - PHIL path (default): settings come from processing.phil (single source).
        #  - --legacy / --bash:   settings come from xds_input_parameters.txt.
        if args.legacy or args.bash:
            if args.xdsparams and os.path.isfile(args.xdsparams):
                params.update(_read_xds_params(args.xdsparams))
            else:
                if data_path is None:
                    if not args.data:
                        sys.exit(
                            "ERROR: -d/--data is required when using a --pinlist but not using --xdsparams"
                        )
                    data_path = DataFinder(paths=args.data).paths[0]
                args.data = data_path
                ParamWriter(args=args).build_parameters()
                params.update(_read_xds_params("xds_input_parameters.txt"))
        else:
            params.update(_params_from_phil(phil))

        project_dir = os.path.abspath(args.output)
        procpath    = os.path.join(project_dir, "processing")
        omnibus     = os.path.join(project_dir, "processing.log")
        sentinel_submitted = False

        if args.bash:        # run Galen's original bash scripts for XDS processing
            if (os.path.isfile('./pin_list.txt') and os.path.isfile('./xds_input_parameters.txt')):
                script_path = os.path.join(os.path.abspath(params["script_dir"]), 'xds_pipeline_prep.sh')
                subprocess.run(script_path)
            else:
                print("ERROR: pin_list.txt and/or xds_input_parameters.txt not found!")
                exit()
        else:
            if args.xds_root:
                params["xds_root"] = args.xds_root
            main_job_id = None
            if not args.legacy:
                # Snapshot the effective PHIL actually used for this run (after CLI
                # overrides), with the resolved data/output injected so the file is
                # self-contained and re-runnable.  Written on every run, even when
                # identical to the defaults; processing_default.phil (from --dry_run)
                # remains the pristine template.
                _snap = ["processing {", "  input {"]
                if args.data:
                    _snap.append("    data = " + " ".join(f'"{d}"' for d in args.data))
                _snap.append(f'    pinlist = "{args.pinlist or "pin_list.txt"}"')
                _snap += ["  }", "  output {", f'    directory = "{project_dir}"', "  }", "}"]
                phil.adopt_phil_string("\n".join(_snap) + "\n")
                # Drop the shipped empty isoform template when a real isoform was
                # supplied (CLI/file), so the as-run record (and the phil_string
                # below) is not cluttered with the all-None template.
                phil.prune_empty_isoforms()
                with open(_DEFAULT_PHIL, "w") as _f:
                    _f.write(phil.full_phil_string())
                print(f"Wrote {_DEFAULT_PHIL} (effective settings for this run)")
                log_phil_diff(phil, omnibus)
            with contextlib.redirect_stdout(_Tee(sys.stdout, omnibus)):
                if args.legacy:
                    RunProcess(params).run()
                else:
                    params["phil_string"] = phil.diff_phil_string()
                    main_job_id = ProcessingOrchestrator(params).run()
            # All finalization (filtered qrefine_list.txt + refine.phil) is done by
            # an SGE completion sentinel held on the main array, so it runs on job
            # completion in every mode -- interactive, --silent, or after the
            # submitting shell disconnects.  The sentinel is the sole arbiter of
            # completion; the foreground watcher below is display-only.
            if not args.legacy and main_job_id:
                ProcessingSentinel(project_dir, no_filter=args.no_filter).submit(main_job_id)
                sentinel_submitted = True

        if args.silent or args.dry_run:
            return

        watcher = Watcher(procpath=procpath)
        if args.no_prog_log:
            while True:
                watcher.tail_logs(log_file=omnibus)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    still_running = watcher.show_progress()
                if not still_running:
                    break
                time.sleep(1)
        else:
            _watch_with_bar(watcher, omnibus)
        watcher.tail_logs(log_file=omnibus)
        print()
        # The file-structure sweep above is idle; wait for the held SGE sentinel
        # to record completion (it also writes the filtered qrefine_list.txt +
        # refine.phil) before declaring the run done.  Safe to Ctrl-C.
        if sentinel_submitted:
            if not wait_for_sentinel(omnibus):
                print("Sentinel marker not seen within the timeout; it will still "
                      "finalize on the cluster (qrefine_list.txt + refine.phil).")
        watcher.print_summary(omnibus=omnibus)
        # Display-only: show the live results table.  The authoritative filtered
        # qrefine_list.txt + refine.phil are written by the sentinel on completion.
        with contextlib.redirect_stdout(_Tee(sys.stdout, omnibus)):
            scraper = ProcessingScraper(procpath)
            scraper.scrape_all()
            scraper.print_info()
    except KeyboardInterrupt:
        print("\nInterrupted. Any submitted jobs are still running on the cluster.")
        sys.exit(1)


if __name__ == "__main__":
    main()
