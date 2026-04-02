import os
import sys
import matplotlib.pyplot as plt


class PinCollector(object):
    
    def __init__(self, dst=None):
        dst = dst[:-1] if dst.endswith('/') else dst
        self.main_path = os.path.abspath(dst)
        print (f"debug: {self.main_path}")
        self.pin_list = []
    
    def collect_pins(self):
        subfolders = sorted(os.listdir(self.main_path))
        for sf in subfolders:
            data_folder = self.main_path.split("/")[-1]
            pin_line = f"{data_folder},{sf}"
            print (pin_line)
            self.pin_list.append(pin_line)
        self.write_to_file()
    
    def write_to_file(self):
        filepath=f"{os.path.abspath(os.curdir)}/pin_list.txt"
        with open(filepath, 'w') as plf:
            for ln in self.pin_list:
                print (ln)
                plf.write(f"{ln}\n")
            print(filepath)


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


    def plot_unit_cells(self):
        """Collect all 'Average unit cell' entries from self.results,
        split into six parameter lists (a, b, c, alpha, beta, gamma),
        and display a 3x2 matplotlib plot (a,b,c left; alpha,beta,gamma right).
        """
        unit_cells_raw = [r['Average unit cell'] for r in self.results if r.get('outcome') == 'completed' and r.get('Average unit cell')]

        # convert to float lists, skip malformed
        unit_cells = []
        for uc in unit_cells_raw:
            try:
                vals = [float(x) for x in uc[:6]]
            except Exception:
                continue
            if len(vals) == 6:
                unit_cells.append(vals)

        if not unit_cells:
            print("No valid 'Average unit cell' entries found in results.")
            return

        # build separate lists
        a_vals, b_vals, c_vals, alpha_vals, beta_vals, gamma_vals = zip(*unit_cells)

        # Use GridSpec with nested subgrids so histograms resize with the figure
        fig = plt.figure(figsize=(10, 9), constrained_layout=True)
        outer_gs = fig.add_gridspec(3, 2)

        # prepare pairs for plotting: (row, col, x_vals, y_vals, xlabel, ylabel, title)
        pairs = [
            (0, 0, a_vals, b_vals, 'a', 'b', 'a vs b'),
            (1, 0, b_vals, c_vals, 'b', 'c', 'b vs c'),
            (2, 0, a_vals, c_vals, 'a', 'c', 'a vs c'),
            (0, 1, alpha_vals, beta_vals, 'alpha', 'beta', 'alpha vs beta'),
            (1, 1, beta_vals, gamma_vals, 'beta', 'gamma', 'beta vs gamma'),
            (2, 1, alpha_vals, gamma_vals, 'alpha', 'gamma', 'alpha vs gamma'),
        ]

        # use the first color from the default color cycle (match 'a') for all plots
        _colors = plt.rcParams['axes.prop_cycle'].by_key().get('color', ['C0'])
        base_color = _colors[0]

        for idx, (r, c, x_vals, y_vals, xl, yl, title) in enumerate(pairs):
            # create a nested GridSpec: small top row for x-hist, main row for scatter and right hist
            cell_gs = outer_gs[r, c].subgridspec(2, 2, height_ratios=[0.25, 1.0], width_ratios=[1.0, 0.25])
            top_ax = fig.add_subplot(cell_gs[0, 0])
            scatter_ax = fig.add_subplot(cell_gs[1, 0])
            right_ax = fig.add_subplot(cell_gs[1, 1])

            color = base_color
            scatter_ax.scatter(x_vals, y_vals, marker='o', s=10, color=color)
            scatter_ax.set_xlabel(xl)
            scatter_ax.set_ylabel(yl)
            scatter_ax.set_title(title)
            scatter_ax.grid(True)

            # top histogram (x-values)
            top_ax.hist(x_vals, bins=20, color=color, alpha=0.6)
            top_ax.tick_params(axis='x', labelbottom=False)
            top_ax.tick_params(axis='y', left=False, labelleft=False)
            # remove box/frame from histogram axes
            top_ax.patch.set_alpha(0)

            # right histogram (y-values)
            right_ax.hist(y_vals, bins=20, orientation='horizontal', color=color, alpha=0.6)
            right_ax.tick_params(axis='y', labelleft=False)
            right_ax.tick_params(axis='x', bottom=False, labelbottom=False)
            right_ax.patch.set_alpha(0)

        # save and show
        fig.savefig('unit_cells.png', bbox_inches='tight')
        plt.show()


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

    def __init__(self, args=None):
        self.procpath = os.path.abspath(args.procpath) if args.procpath is not None else os.path.abspath(os.curdir)
        self.pincode = args.pincode
        self.details = args.details
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
                self.datasets[parts[1].strip()] = None
        folders = [name for name in os.listdir(self.procpath)
                   if os.path.isdir(os.path.join(self.procpath, name))]
        for pin in self.datasets:
            pin_folder = next((f for f in folders if pin in f), None)
            if pin_folder is None:
                self.datasets[pin] = {'none': {"NOT PROCESSED", None}}
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

    def show_count(self):
        self.watch()
        all_datasets = [v for sub in self.datasets.values() for v in sub.values()]
        not_processed_count = len([d for d in all_datasets if "NOT PROCESSED" in d])
        processed = [d for d in all_datasets if "NOT PROCESSED" not in d]
        total_count = len(processed)
        failed_count = len([d for d in processed if d[0] == "FAILED"])
        finished_count = len([d for d in processed if d[0] == "FINISHED"])
        print(f"\rTotal: {total_count} | Not Processed: {not_processed_count} | Failed: {failed_count} | Finished: {finished_count}", end="", flush=True)

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
        else:
            self.show_count()
