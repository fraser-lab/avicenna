import os
import sys
from datetime import datetime
# import matplotlib.pyplot as plt

# Bravais lattice → list of (sg_number, sg_name) tuples.
# Derived from xds_lattices.txt in the processing pipeline scripts directory.
BRAVAIS_LATTICES = {
    "aP": [(1,   "P1")],
    "mP": [(3,   "P121"),   (4,   "P1211")],
    "mC": [(5,   "C2")],
    "mI": [(5,   "C2")],
    "oP": [(16,  "P222"),   (17,  "P2221"),  (18,  "P21212"), (19,  "P212121")],
    "oC": [(21,  "C222"),   (20,  "C2221")],
    "oF": [(22,  "F222")],
    "oI": [(23,  "I222"),   (24,  "I212121")],
    "tP": [(75,  "P4"),     (76,  "P41"),    (77,  "P42"),    (78,  "P43"),
           (89,  "P422"),   (90,  "P4212"),  (91,  "P4122"),  (92,  "P41212"),
           (93,  "P4222"),  (94,  "P42212"), (95,  "P4322"),  (96,  "P43212")],
    "tI": [(79,  "I4"),     (80,  "I41"),    (97,  "I422"),   (98,  "I4122")],
    "hP": [(143, "P3"),     (144, "P31"),    (145, "P32"),    (149, "P312"),
           (150, "P321"),   (151, "P3112"),  (152, "P3121"),  (153, "P3212"),
           (154, "P3221"),  (168, "P6"),     (169, "P61"),    (170, "P65"),
           (171, "P62"),    (172, "P64"),    (173, "P63"),    (177, "P622"),
           (178, "P6122"),  (179, "P6522"),  (180, "P6222"),  (181, "P6422"),
           (182, "P6322")],
    "hR": [(146, "R3"),     (155, "R32")],
    "cP": [(195, "P23"),    (198, "P213"),   (207, "P432"),   (208, "P4232"),
           (212, "P4332"),  (213, "P4132")],
    "cF": [(196, "F23"),    (209, "F432"),   (210, "F4132")],
    "cI": [(197, "I23"),    (199, "I213"),   (211, "I432"),   (214, "I4132")],
}


class DataFinder(object):

    def __init__(self, paths):
        resolved = []
        for p in paths:
            p = str(p)
            if os.path.isfile(p) and p.endswith('.txt'):
                with open(p) as fh:
                    for line in fh:
                        line = line.strip()
                        if line and os.path.isdir(line):
                            resolved.append(os.path.abspath(line))
            elif os.path.isdir(p):
                resolved.append(os.path.abspath(p.rstrip('/')))
        self.paths = resolved
        self.pin_list = []
        self.image_dir = None

    def collect(self):
        """Validate paths, collect pin codes, write pin_list.txt.
        Returns (pin_list, image_dir, first_resolved_path).
        """
        if not self.paths:
            sys.exit("ERROR: no valid data directories found in provided paths.")
        parents = {os.path.dirname(p) for p in self.paths}
        if len(parents) > 1:
            sys.exit(
                "ERROR: all data paths must share the same parent directory.\n"
                "Conflicting parents found:\n" +
                "\n".join(f"  {p}" for p in sorted(parents))
            )
        self.image_dir = parents.pop()
        for path in self.paths:
            for sf in sorted(os.listdir(path)):
                if not os.path.isdir(os.path.join(path, sf)):
                    continue
                pin_line = f"{path},{sf}"
                print(pin_line)
                self.pin_list.append(pin_line)
        filepath = os.path.join(os.path.abspath(os.curdir), "pin_list.txt")
        with open(filepath, 'w') as fh:
            for pin in self.pin_list:
                fh.write(f"{pin}\n")
        print(filepath)
        return self.pin_list, self.image_dir, self.paths[0]


class ParamWriter(object):
    def __init__(self, args):
        dst = str(args.data)[:-1] if str(args.data).endswith('/') else str(args.data)
        self.main_path = os.path.abspath(dst)

    def build_parameters(self):
        image_dir = os.path.dirname(self.main_path)
        xds_params = {
            'script_dir':            '../scripts',
            'image_dir':             image_dir,
            'detector_edge':         '0.8',
            'indexing_images':       'first_half',
            'try_alt_sym':           'all',
            'ncpu':                  '8',
            'no_symmetry':           'false',
            'scaling':               'correct',
            'recycle_xds':           'false',
            'refine_cell_integrate': 'both',
            'res_limit':             'cc12',
            'cleanup':               'partial',
        }
        xds_param_filepath = f"{os.path.abspath(os.curdir)}/xds_input_parameters.txt"
        print(xds_param_filepath)
        with open(xds_param_filepath, 'w') as xpf:
            for key, value in xds_params.items():
                xpf.write(f"{key}={value}\n")


class Scraper(object):
    def __init__(self, result_path):
        self.result_path = result_path
        self.results = {}

    def _find_aimless_log(self, index_path):
        """Given an index_X_Y folder, return the path to the appropriate
        10_aimless.log, or None if no completed job is found.
        """
        # Flat case: job completed directly under index_path
        if os.path.isdir(os.path.join(index_path, '13_xtriage')):
            logs = []
            for d in os.listdir(index_path):
                if d.endswith('_aimless') and os.path.isdir(os.path.join(index_path, d)):
                    log = os.path.join(index_path, d, f'{d}.log')
                    if os.path.isfile(log):
                        try:
                            logs.append((int(d.split('_')[0]), log))
                        except ValueError:
                            pass
            return max(logs, key=lambda x: x[0])[1] if logs else None

        # Force dir case: find *_force subdirs, sorted by integer prefix
        # (prefix encodes symmetry order: 1 = highest symmetry)
        force_dirs = sorted(
            [d for d in os.listdir(index_path)
             if d.endswith('_force') and os.path.isdir(os.path.join(index_path, d))],
            key=lambda d: int(d.split('_')[0])
        )
        for force_dir in force_dirs:
            force_path = os.path.join(index_path, force_dir)
            if os.path.isdir(os.path.join(force_path, '13_xtriage')):
                log = os.path.join(force_path, '10_aimless', '10_aimless.log')
                return log if os.path.isfile(log) else None

        return None

    def find_results(self):
        print(f"Looking in {self.result_path}...")
        results = []
        for dataset_name in sorted(os.listdir(self.result_path)):
            dataset_path = os.path.join(self.result_path, dataset_name)
            if not os.path.isdir(dataset_path):
                continue
            parts = dataset_name.split('_')
            pin_code     = parts[-3]
            dataset_code = '_'.join(parts[-3:-1])
            for result_type in os.listdir(dataset_path):
                result_type_path = os.path.join(dataset_path, result_type)
                if not os.path.isdir(result_type_path):
                    continue
                for index_name in sorted(os.listdir(result_type_path)):
                    index_path = os.path.join(result_type_path, index_name)
                    if not os.path.isdir(index_path) or not index_name.startswith('index_'):
                        continue
                    results.append({
                        'pin_code'     : pin_code,
                        'dataset_code' : dataset_code,
                        'proc_path'    : dataset_path,
                        'result_path'  : result_type_path,
                        'aimless_log'  : self._find_aimless_log(index_path),
                        'outcome'      : 'unprocessed',
                    })
        return results

    def scrape_one_log(self, info_dict):
        logfile = info_dict.get('aimless_log')
        if not logfile or not os.path.isfile(logfile):
            return None

        with open(logfile, 'r') as lf:
            contents = lf.readlines()

        multi_params = {
                'High resolution limit'                 : None,
                'Rpim (within I+/I-)'                   : None,
                'Mn(I) half-set correlation CC(1/2)'    : None,
                'Completeness'                          : None,
                'Multiplicity'                          : None,
                'Average unit cell'                     : None,
                }
        single_params = {
                'Space group'                           : None,
                'Average mosaicity'                     : None,
                }

        for c in contents[-100:]:
            for key, value in single_params.items():
                if key in c:
                    single_params[key] = c.replace(key, '').replace(" ", "").strip()
            for key, value in multi_params.items():
                if key in c:
                    splits = [sp for sp in c.strip().split(' ') if sp != '' and (sp.strip().isdigit() or sp.replace('.','',1).isdigit())]
                    multi_params[key] = splits
        info_dict.update(single_params)
        info_dict.update(multi_params)
        return info_dict


    def scrape_all(self):
        results = self.find_results()
        for result in results:
            updated_result = self.scrape_one_log(info_dict=result)
            if updated_result is None:
                result['outcome'] = 'failed'
            else:
                result.update(updated_result)
                result['outcome'] = 'completed'
        self.results = results


    def print_info(self):
        for result in self.results:
            if result['outcome'] == 'completed':
                try:
                    comp = f"{result['Completeness'][0]:>5} ({result['Completeness'][-1]})"
                    mult = f"{result['Multiplicity'][0]:>5} ({result['Multiplicity'][-1]})"
                    rpim = f"{result['Rpim (within I+/I-)' ][0]:>5} ({result['Rpim (within I+/I-)' ][-1]})"
                    cell = f"{' '.join(result['Average unit cell'])}"
                    print (
                        f"{result['dataset_code']:10} ({os.path.basename(result['result_path'])}) SG: {result['Space group']:<10} CELL: {cell:22} RES: {result['High resolution limit'][-1]:8} COMP: {comp:<14} MULT: {mult:<14} RPIM: {rpim:<14}" 
                    )
                except KeyError as e:
                    print (f"ERROR: {result['dataset_code']} Missing item: {str(e)}")
                except TypeError as e:
                    print (f"ERROR: {result['dataset_code']} Missing stats!")
            else:
                print (f"{result['dataset_code']} ({os.path.basename(result['result_path'])}) FAILED")



class Watcher(object):

    # Tag → Watcher status mapping
    TAG_STATUS = {
        "START"  : "RUNNING",
        "PREP"   : "PREPARING",
        "RUN"    : "RUNNING",
        "FORCE"  : "RUNNING_FORCE",
        "DONE"   : "FINISHED",
        "ERROR"  : "FAILED",
    }

    def __init__(self, procpath=None, pincode=None, details=False):
        self.procpath = os.path.abspath(procpath) if procpath is not None else os.path.abspath(os.curdir)
        self.pincode = pincode
        self.details = details
        self._log_offsets = {}   # path → number of lines already printed
        self.get_info()

    def get_info(self):
        if not os.path.isdir(self.procpath):
            sys.exit(f"Error: processing directory '{self.procpath}' does not exist.")
        pin_list_path = os.path.join(self.procpath, "pin_list.txt")
        if not os.path.isfile(pin_list_path):
            sys.exit(f"Error: 'pin_list.txt' not found in '{self.procpath}'.")
        self.datasets = {}
        with open(pin_list_path, 'r') as f:
            for line in f:
                parts = line.split(',')
                collection = os.path.basename(parts[0].strip()).replace('_transfer', '')
                self.datasets[f"{collection}_{parts[1].strip()}"] = None
        folders = [name for name in os.listdir(self.procpath)
                   if os.path.isdir(os.path.join(self.procpath, name))]
        for pin in self.datasets:
            pin_folder = next((f for f in folders if pin in f), None)
            if pin_folder is None:
                self.datasets[pin] = {'none': {"REJECTED", None}}
            else:
                pin_folder_path = os.path.join(self.procpath, pin_folder)
                subfolders = [name for name in os.listdir(pin_folder_path)
                              if os.path.isdir(os.path.join(pin_folder_path, name))]
                self.datasets[pin] = {sf: (None, None) for sf in subfolders}

    def _pipeline_logs(self, result_type_path):
        """Find all pipeline.log files under result_type_path, searching
        index_*/ dirs at the top level and *_force/ dirs one level deeper.
        Returns a list of absolute paths."""
        logs = []
        for entry in os.listdir(result_type_path):
            index_path = os.path.join(result_type_path, entry)
            if not os.path.isdir(index_path) or not entry.startswith('index_'):
                continue
            force_logs = []
            for sub in os.listdir(index_path):
                if sub.endswith('_force'):
                    force_log = os.path.join(index_path, sub, 'pipeline.log')
                    if os.path.isfile(force_log):
                        force_logs.append(force_log)
            if force_logs:
                logs.extend(force_logs)
            else:
                flat_log = os.path.join(index_path, 'pipeline.log')
                if os.path.isfile(flat_log):
                    logs.append(flat_log)
        return logs

    def _read_pipeline_log(self, log_path):
        """Return (status, message) from the last meaningful line of pipeline.log."""
        try:
            with open(log_path) as fh:
                lines = [l.rstrip() for l in fh if l.strip()]
        except OSError:
            return (None, None)
        for line in reversed(lines):
            # format: [YYYY-MM-DD HH:MM:SS] TAG        message
            try:
                _, _, tag, *rest = line.split(None, 3)
                tag = tag.strip()
                msg = rest[0].strip() if rest else ""
                status = self.TAG_STATUS.get(tag)
                if status:
                    return (status, msg)
            except ValueError:
                continue
        return (None, None)

    def watch(self):
        folders = [name for name in os.listdir(self.procpath)
                   if os.path.isdir(os.path.join(self.procpath, name))]
        for pin in self.datasets:
            pin_folder = next((f for f in folders if pin in f), None)
            if pin_folder is None:
                continue
            pin_folder_path = os.path.join(self.procpath, pin_folder)
            for psf_name in self.datasets[pin]:
                if "REJECTED" in self.datasets[pin][psf_name]:
                    continue
                psf = os.path.join(pin_folder_path, psf_name)
                logs = self._pipeline_logs(psf)
                if not logs:
                    continue
                # Aggregate: DONE only if all logs are DONE; any FAILED → FAILED;
                # otherwise use the most recent non-None status
                statuses = [self._read_pipeline_log(l) for l in logs]
                statuses = [(s, m) for s, m in statuses if s is not None]
                if not statuses:
                    continue
                if any(s == "FAILED" for s, _ in statuses):
                    failed_msg = next(m for s, m in statuses if s == "FAILED")
                    self.datasets[pin][psf_name] = ("FAILED", failed_msg)
                elif all(s == "FINISHED" for s, _ in statuses):
                    self.datasets[pin][psf_name] = ("FINISHED", None)
                else:
                    # pick most informative running status
                    last_s, last_m = statuses[-1]
                    self.datasets[pin][psf_name] = (last_s, last_m)

    def tail_logs(self):
        """Print any new lines added to pipeline.log files since last call.
        Returns the list of new lines printed."""
        new_lines = []
        for pin in self.datasets:
            pin_folder_path = next(
                (os.path.join(self.procpath, f) for f in os.listdir(self.procpath)
                 if pin in f and os.path.isdir(os.path.join(self.procpath, f))),
                None
            )
            if pin_folder_path is None:
                continue
            for psf_name in self.datasets[pin]:
                psf = os.path.join(pin_folder_path, psf_name)
                if not os.path.isdir(psf):
                    continue
                for log_path in self._pipeline_logs(psf):
                    try:
                        with open(log_path) as fh:
                            all_lines = fh.readlines()
                    except OSError:
                        continue
                    offset = self._log_offsets.get(log_path, 0)
                    for line in all_lines[offset:]:
                        print(line, end="", flush=True)
                        new_lines.append(line)
                    self._log_offsets[log_path] = len(all_lines)
        return new_lines

    def is_running(self):
        """Return True if any jobs are still in progress (no output)."""
        self.watch()
        all_datasets = [v for sub in self.datasets.values() for v in sub.values()]
        processed    = [d for d in all_datasets if "REJECTED" not in d]
        finished     = len([d for d in processed if d[0] == "FINISHED"])
        failed       = len([d for d in processed if d[0] == "FAILED"])
        return (len(processed) - finished - failed) > 0

    def tail_log(self):
        """Print any new lines added to processing.log since the last call.
        Returns the list of new lines printed."""
        omnibus = os.path.join(self.procpath, "processing.log")
        new_lines = []
        if not os.path.isfile(omnibus):
            return new_lines
        try:
            with open(omnibus) as fh:
                all_lines = fh.readlines()
        except OSError:
            return new_lines
        offset = self._log_offsets.get(omnibus, 0)
        for line in all_lines[offset:]:
            print(line, end="", flush=True)
            new_lines.append(line)
        self._log_offsets[omnibus] = len(all_lines)
        return new_lines

    def show_progress(self):
        self.watch()
        # Count at pin level for the progress bar
        finished = failed = in_progress = rejected = 0
        for pin, subfolders in self.datasets.items():
            statuses = list(subfolders.values())
            if any("REJECTED" in s for s in statuses):
                rejected += 1
            elif any(s[0] not in ("FINISHED", "FAILED") for s in statuses):
                in_progress += 1
            elif any(s[0] == "FAILED" for s in statuses):
                failed += 1
            else:
                finished += 1
        total = len(self.datasets)

        bar_width = total if total <= 40 else 40
        if total > 0:
            n_failed   = round(failed      / total * bar_width)
            n_not_proc = round(rejected    / total * bar_width)
            n_active   = round(in_progress / total * bar_width)
            if n_active == 0 and in_progress > 0:
                n_active = 1
            n_done     = max(0, bar_width - n_failed - n_not_proc - n_active)
        else:
            n_done = n_failed = n_active = 0
            n_not_proc = bar_width

        bar  = "#" * n_done + "!" * n_failed + "-" * n_active + "X" * n_not_proc
        line = (f"\r({finished + failed + rejected}/{total}) [{bar}] "
                f"{finished} processed | {failed} failed | {in_progress} running | {rejected} rejected")
        print(f"{line:100}", end="", flush=True)

        return in_progress > 0

    def show_count(self):
        self.watch()
        all_datasets = [v for sub in self.datasets.values() for v in sub.values()]
        rejected_count = len([d for d in all_datasets if "REJECTED" in d])
        processed = [d for d in all_datasets if "REJECTED" not in d]
        total_count = len(processed)
        failed_count = len([d for d in processed if d[0] == "FAILED"])
        finished_count = len([d for d in processed if d[0] == "FINISHED"])
        print(f"\rTotal: {total_count} | Rejected: {rejected_count} | Failed: {failed_count} | Finished: {finished_count}", end="", flush=True)

    def show_details(self):
        self.watch()
        msg = []
        for pin, subfolders in self.datasets.items():
            for psf_name, (status, error) in subfolders.items():
                entry = f"{pin} {psf_name}: {status}"
                if error is not None:
                    entry += f" ({error})"
                msg.append(entry)
        print("\n".join(msg) + "\n*************\n")

    def print_summary(self, omnibus=None):
        """Print a completion summary and optionally append it to the omnibus log."""
        all_datasets = [v for sub in self.datasets.values() for v in sub.values()]
        rejected     = len([d for d in all_datasets if "REJECTED" in d])
        processed    = [d for d in all_datasets if "REJECTED" not in d]
        finished     = len([d for d in processed if d[0] == "FINISHED"])
        failed       = len([d for d in processed if d[0] == "FAILED"])
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"[{ts}] Run complete.",
            f"[{ts}] Processed: {finished} | Failed: {failed} | Rejected: {rejected}",
            f"[{ts}] =======================",
        ]
        for line in lines:
            print(line, flush=True)
        if omnibus:
            with open(omnibus, "a") as fh:
                for line in lines:
                    fh.write(line + "\n")

    def run(self):
        if self.details:
            self.show_details()
            return True
        return self.show_progress()
