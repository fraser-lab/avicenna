import os
import sys
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
        self.args = args

    def build_parameters(self):
        image_dir = os.path.dirname(self.main_path)
        no_symmetry = not (os.path.isfile(str(self.args.refhkl)) or os.path.isfile(str(self.args.refmtz)))

        
        xds_params = {
            'script_dir'            : os.path.abspath(self.args.scripts),
            'image_dir'             : image_dir,
            'detector_edge'         : self.args.edge,
            'indexing_images'       : self.args.idxrange,
            'try_alt_sym'           : self.args.altsym,
            'ncpu'                  : self.args.ncpu,
            'no_symmetry'           : str(no_symmetry).lower(),
            'scaling'               : self.args.scaling,
            'recycle_xds'           : str(self.args.recycle).lower(),
            'refine_cell_integrate' : self.args.refcell,
            'res_limit'             : self.args.reslim,
            'cleanup'               : self.args.cleanup,
        }
        if self.args.refhkl is not None:
            xds_params['ref_hkl'] = self.args.refhkl
        if self.args.refmtz is not None:
            xds_params['ref_mtz'] = self.args.refmtz
        xds_param_filepath = f"{os.path.abspath(os.curdir)}/xds_input_parameters.txt"
        print (xds_param_filepath)
        with open(xds_param_filepath, 'w') as xpf:
            for key, value in xds_params.items():
                xpf.write(f"{key}={value}\n")


class Scraper(object):
    def __init__(self, result_path):
        self.result_path = result_path
        self.results = {}
    
    def find_results(self):
        print (f"Looking in {self.result_path}...")
        paths = sorted([os.path.join(self.result_path, p) for p in os.listdir(self.result_path) if os.path.isdir(os.path.join(self.result_path, p)) == True])
        results = []
        for path in paths:
            result_folders = [os.path.join(path, p) for p in os.listdir(path) if os.path.isdir(os.path.join(path, p)) == True]
            for rf in result_folders:
                result = {
                    'pin_code'      : os.path.basename(path).split('_')[-3],
                    'dataset_code'  : '_'.join(os.path.basename(path).split('_')[-3:-1]),
                    'proc_path'     : path,
                    'result_path'   : rf,
                    'outcome'       : 'unprocessed'
                }
                results.append(result)
        return results

    def scrape_one_log(self, info_dict):

        # collect aimless log files and pick the last one
        aimless_log_files = []
        for dirpath, _, files in os.walk(info_dict['result_path']):
            for file in files:
                if 'aimless' in file and file.endswith(".log"):
                    aimless_log_files.append(os.path.join(dirpath, file))

        if len(aimless_log_files) == 0:
            return None

        logfile = sorted(aimless_log_files)[-1]

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


    # def plot_unit_cells(self):
    #     """Collect all 'Average unit cell' entries from self.results,
    #     split into six parameter lists (a, b, c, alpha, beta, gamma),
    #     and display a 3x2 matplotlib plot (a,b,c left; alpha,beta,gamma right).
    #     """
    #     unit_cells_raw = [r['Average unit cell'] for r in self.results if r.get('outcome') == 'completed' and r.get('Average unit cell')]

    #     # convert to float lists, skip malformed
    #     unit_cells = []
    #     for uc in unit_cells_raw:
    #         try:
    #             vals = [float(x) for x in uc[:6]]
    #         except Exception:
    #             continue
    #         if len(vals) == 6:
    #             unit_cells.append(vals)

    #     if not unit_cells:
    #         print("No valid 'Average unit cell' entries found in results.")
    #         return

    #     # build separate lists
    #     a_vals, b_vals, c_vals, alpha_vals, beta_vals, gamma_vals = zip(*unit_cells)

    #     # Use GridSpec with nested subgrids so histograms resize with the figure
    #     fig = plt.figure(figsize=(10, 9), constrained_layout=True)
    #     outer_gs = fig.add_gridspec(3, 2)

    #     # prepare pairs for plotting: (row, col, x_vals, y_vals, xlabel, ylabel, title)
    #     pairs = [
    #         (0, 0, a_vals, b_vals, 'a', 'b', 'a vs b'),
    #         (1, 0, b_vals, c_vals, 'b', 'c', 'b vs c'),
    #         (2, 0, a_vals, c_vals, 'a', 'c', 'a vs c'),
    #         (0, 1, alpha_vals, beta_vals, 'alpha', 'beta', 'alpha vs beta'),
    #         (1, 1, beta_vals, gamma_vals, 'beta', 'gamma', 'beta vs gamma'),
    #         (2, 1, alpha_vals, gamma_vals, 'alpha', 'gamma', 'alpha vs gamma'),
    #     ]

    #     # use the first color from the default color cycle (match 'a') for all plots
    #     _colors = plt.rcParams['axes.prop_cycle'].by_key().get('color', ['C0'])
    #     base_color = _colors[0]

    #     for idx, (r, c, x_vals, y_vals, xl, yl, title) in enumerate(pairs):
    #         # create a nested GridSpec: small top row for x-hist, main row for scatter and right hist
    #         cell_gs = outer_gs[r, c].subgridspec(2, 2, height_ratios=[0.25, 1.0], width_ratios=[1.0, 0.25])
    #         top_ax = fig.add_subplot(cell_gs[0, 0])
    #         scatter_ax = fig.add_subplot(cell_gs[1, 0])
    #         right_ax = fig.add_subplot(cell_gs[1, 1])

    #         color = base_color
    #         scatter_ax.scatter(x_vals, y_vals, marker='o', s=10, color=color)
    #         scatter_ax.set_xlabel(xl)
    #         scatter_ax.set_ylabel(yl)
    #         scatter_ax.set_title(title)
    #         scatter_ax.grid(True)

    #         # top histogram (x-values)
    #         top_ax.hist(x_vals, bins=20, color=color, alpha=0.6)
    #         top_ax.tick_params(axis='x', labelbottom=False)
    #         top_ax.tick_params(axis='y', left=False, labelleft=False)
    #         # remove box/frame from histogram axes
    #         top_ax.patch.set_alpha(0)

    #         # right histogram (y-values)
    #         right_ax.hist(y_vals, bins=20, orientation='horizontal', color=color, alpha=0.6)
    #         right_ax.tick_params(axis='y', labelleft=False)
    #         right_ax.tick_params(axis='x', bottom=False, labelbottom=False)
    #         right_ax.patch.set_alpha(0)

    #     # save and show
    #     fig.savefig('unit_cells.png', bbox_inches='tight')
    #     plt.show()


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
    tells = {
        "1_xds.log"        : "XDS(1)",
        "4_aimless.log"    : "AIMLESS(1)",
        "5_xds.log"        : "XDS(2)",
        "7_aimless.log"    : "AIMLESS(2)",
        "13_xtriage.log"   : "FINISHED",
        }

    def find_errors(self, logfile):
        with open(logfile, 'r') as lf:
            contents = lf.readlines()
        for c in contents[-10:]:
            if " ERROR " in c:
                return c.strip()
        return None

    def __init__(self, procpath=None, pincode=None, details=False):
        self.procpath = os.path.abspath(procpath) if procpath is not None else os.path.abspath(os.curdir)
        self.pincode = pincode
        self.details = details
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


    def watch(self):
        folders = [name for name in os.listdir(self.procpath)
                   if os.path.isdir(os.path.join(self.procpath, name))]
        for pin in self.datasets:
            pin_folder = next((f for f in folders if pin in f), None)
            if pin_folder is None:
                continue
            pin_folder_path = os.path.join(self.procpath, pin_folder)
            for psf_name in self.datasets[pin]:
                psf = os.path.join(pin_folder_path, psf_name)
                logfiles = []
                for dirname, _, files in os.walk(psf):
                    local_logfile_splits = []
                    for f in files:
                        if f in self.tells.keys():
                            f_split = f.split("_")
                            local_logfile_splits.append([dirname, f_split[0], f_split[1]])
                        logfiles.extend(local_logfile_splits)
                if len(logfiles) > 0:
                    logs = sorted(logfiles, key=lambda x: int(x[1]))
                    last_log = os.path.join(logs[-1][0], f"{logs[-1][1]}_{logs[-1][2]}")
                    last_logfile = os.path.basename(last_log)
                    if self.find_errors(last_log) is not None:
                        self.datasets[pin][psf_name] = ("FAILED", f"error in {last_logfile}")
                    else:
                        self.datasets[pin][psf_name] = (self.tells[last_logfile], None)

    def show_progress(self):
        self.watch()
        all_datasets  = [v for sub in self.datasets.values() for v in sub.values()]
        rejected   = len([d for d in all_datasets if "REJECTED" in d])
        processed     = [d for d in all_datasets if "REJECTED" not in d]
        finished      = len([d for d in processed if d[0] == "FINISHED"])
        failed        = len([d for d in processed if d[0] == "FAILED"])
        in_progress   = len(processed) - finished - failed
        total         = len(all_datasets)

        bar_width = total if total <= 40 else 40
        if total > 0:
            n_done     = round(finished    / total * bar_width)
            n_failed   = round(failed      / total * bar_width)
            n_not_proc = round(rejected / total * bar_width)
            n_active   = bar_width - n_done - n_failed - n_not_proc
        else:
            n_done = n_failed = n_active = 0
            n_not_proc = bar_width

        bar  = "#" * n_done + "!" * n_failed + "-" * n_active + "X" * n_not_proc
        line = (f"\r[{bar}] {finished + failed + rejected}/{total} done | "
                f"{finished} processed | {failed} failed | {in_progress} running | {rejected} rejected")
        print(f"{line:100}", end="", flush=True)

        return in_progress > 0 or rejected > 0

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

    def run(self):
        if self.details:
            self.show_details()
            return True
        return self.show_progress()
