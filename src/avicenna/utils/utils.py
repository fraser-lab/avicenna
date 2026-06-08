import math
import os
import pathlib
import sys
import threading
import time
from datetime import datetime
# import matplotlib.pyplot as plt

# Suffixes appended to crystal directory names to indicate cell-refinement mode.
# New data uses '_refined'; older data on disk may carry '_true', '_false', or '_both'.
# Listed longest-first so the stripping loop matches the most-specific suffix.
_CELL_REFINED_SUFFIXES = ('_refined', '_true', '_false', '_both')

_LOG_DECORATIONS = {
    "ERROR":    "***",
    "COMPLETE": "!!!",
}
_TAG_WIDTH = 12


def format_log_line(tag, message, context=None, context_width=None, forced_dir=None):
    """Return a formatted pipeline log line.

    The tag field is fixed-width (_TAG_WIDTH). Tags in _LOG_DECORATIONS get
    a right-aligned decoration within that field. An optional context string
    (e.g. a job_id) is inserted in brackets between the tag field and the
    message. If context_width is given, the context is left-padded to that
    width so the message column stays aligned across all log lines.
    forced_dir, when present, is inserted in brackets immediately before the
    message to identify forced-integration retry jobs.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    decoration = _LOG_DECORATIONS.get(tag, "")
    tag_field = f"{tag}{decoration:>{_TAG_WIDTH - len(tag)}}"
    if context is not None:
        ctx_str = f"{context:<{context_width}}" if context_width else context
        ctx = f"[ {ctx_str} ]  "
    else:
        ctx = ""
    prefix = f"[{forced_dir}]  " if forced_dir else ""
    return f"[{ts}] {tag_field}  {ctx}{prefix}{message}\n"

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

_SG_BY_NAME:   dict = {}
_SG_BY_NUMBER: dict = {}
for _lat_sgs in BRAVAIS_LATTICES.values():
    for _num, _name in _lat_sgs:
        _SG_BY_NAME[_name.upper()] = (_num, _name)
        _SG_BY_NUMBER[_num]        = (_num, _name)

# Short / alternative symbols that differ from the canonical XDS name.
# Monoclinic: IUCr short symbol omits the identity axis positions.
# Rhombohedral: H-prefix names denote the hexagonal setting used by CCP4/Pointless.
_SG_ALIASES = {
    "P2":  "P121",   # SG 3
    "P21": "P1211",  # SG 4
    "H3":  "R3",     # SG 146, hexagonal setting
    "H32": "R32",    # SG 155, hexagonal setting
}
for _alias, _canonical in _SG_ALIASES.items():
    _SG_BY_NAME[_alias] = _SG_BY_NAME[_canonical]


def sg_lookup(space_group=None, space_group_number=None):
    """Return (number, canonical_name) for a space group specification.

    Pass exactly one argument; raises ValueError if the lookup fails.
    Names are matched case-insensitively with spaces stripped
    (e.g. 'P43212', 'p 43 2 12', 'P 43 21 2' all resolve to SG 96).
    """
    if space_group is not None:
        key = space_group.strip().upper().replace(" ", "")
        if key not in _SG_BY_NAME:
            raise ValueError(
                f"Unknown space group name {space_group!r}. "
                "Use XDS-style names (e.g. 'P43212', 'C2', 'I222')."
            )
        return _SG_BY_NAME[key]
    if space_group_number is not None:
        if space_group_number not in _SG_BY_NUMBER:
            raise ValueError(f"Unknown space group number {space_group_number}.")
        return _SG_BY_NUMBER[space_group_number]
    raise ValueError("Provide space_group or space_group_number.")


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
            'on_indexing_failure':   'auto',
            'retry_mode':            'auto',
            'on_integration_failure': 'reject',
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


class ScraperBase(object):
    def __init__(self, result_path):
        self.result_path = result_path
        self.results = {}

    def find_results(self):
        raise NotImplementedError

    def scrape_one_log(self, info_dict):
        raise NotImplementedError

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
        raise NotImplementedError

    @staticmethod
    def _print_table_mixed(tagged_rows, headers):
        """Print rows as an aligned fixed-width table with two row formats.

        tagged_rows is a list of (is_error, row) pairs:
          - ok rows (is_error=False): full n-tuple matching headers
          - error rows (is_error=True): (*id_cols, msg) where id_cols are the
            leading columns to pad and msg is the error string (may be empty)

        Column widths are computed from headers and ok rows only, so error rows
        do not inflate the column widths. Error rows print their id columns
        padded to the same widths as ok rows, then 'FAILED' and the message
        directly — skipping the empty middle columns entirely.
        """
        if not tagged_rows:
            return
        n = len(headers)
        ok_rows = [row for is_err, row in tagged_rows if not is_err]
        widths = [
            max(len(str(headers[i])), max((len(str(r[i])) for r in ok_rows), default=0))
            for i in range(n - 1)
        ]
        def fmt_ok(row):
            parts = [f'{str(row[i]):<{widths[i]}}' for i in range(n - 1)]
            parts.append(str(row[n - 1]))
            return '  '.join(parts).rstrip()
        def fmt_err(row):
            n_ids = len(row) - 1
            parts = [f'{str(row[i]):<{widths[i]}}' for i in range(n_ids)]
            parts.append('FAILED')
            if row[-1]:
                parts.append(str(row[-1]))
            return '  '.join(parts).rstrip()
        header = fmt_ok(headers)
        print(header)
        print('-' * len(header))
        for is_err, row in tagged_rows:
            print(fmt_err(row) if is_err else fmt_ok(row))


class ProcessingScraper(ScraperBase):

    _ERROR_PATTERNS = ('!!! ERROR', 'Error:', 'FATAL ERROR', 'Sorry:')

    @staticmethod
    def _tail(path, n=50):
        """Read the last n lines of a file without loading the whole file."""
        try:
            with open(path, 'rb') as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - n * 200))
                return f.read().decode('utf-8', errors='replace').splitlines()[-n:]
        except OSError:
            return []

    @staticmethod
    def _find_error(lines):
        """Scan lines in reverse for a known error pattern; return the first match or None."""
        for line in reversed(lines):
            for pat in ProcessingScraper._ERROR_PATTERNS:
                if pat in line:
                    return line.strip()
        return None

    def _diagnose(self, pipeline_subdir):
        """
        Verify the three success conditions for a pipeline subdir.
        Returns (True, None) on success or (False, error_str) on failure.
        Only called when a log was found, so conditions failing here indicate
        a real problem (e.g. freerflag error masked by xtriage completing).
        """
        p = pathlib.Path(pipeline_subdir)

        # Condition 1: XDS produced XDS_ASCII.HKL in any *_xds step directory
        hkl_ok = any(
            (d / 'XDS_ASCII.HKL').is_file()
            for d in p.iterdir()
            if d.is_dir() and d.name.endswith('_xds')
        )
        if not hkl_ok:
            xds_dirs = sorted(
                (d for d in p.iterdir() if d.is_dir() and d.name.endswith('_xds')),
                key=lambda d: int(d.name.split('_')[0]), reverse=True,
            )
            if xds_dirs:
                log = xds_dirs[0] / f'{xds_dirs[0].name}.log'
            else:
                log = None
            excerpt = self._find_error(self._tail(log)) if log and log.is_file() else None
            label = xds_dirs[0].name if xds_dirs else 'xds'
            return False, f"{label}: {excerpt or '(no error found in log)'}"

        # Condition 2: rfree chain produced XDS_ASCII_ctruncate.mtz
        rfree_dirs = [d for d in p.iterdir() if d.is_dir() and d.name.endswith('_rfree')]
        rfree_ok = any((d / 'XDS_ASCII_ctruncate.mtz').is_file() for d in rfree_dirs)
        if not rfree_ok:
            rfree_dir = rfree_dirs[0] if rfree_dirs else None
            logs = sorted(rfree_dir.glob('*_rfree.log')) if rfree_dir else []
            excerpt = self._find_error(self._tail(logs[0])) if logs else None
            label = rfree_dir.name if rfree_dir else 'rfree'
            return False, f"{label}: {excerpt or '(no error found in log)'}"

        # Condition 3: xtriage directory was created
        xtriage_exists = any(
            d.is_dir() and d.name.endswith('_xtriage') for d in p.iterdir()
        )
        if not xtriage_exists:
            step_dirs = sorted(
                (d for d in p.iterdir() if d.is_dir() and d.name[0].isdigit()),
                key=lambda d: int(d.name.split('_')[0]), reverse=True,
            )
            if step_dirs:
                logs = list(step_dirs[0].glob('*.log'))
                excerpt = self._find_error(self._tail(logs[0])) if logs else None
                return False, f"{step_dirs[0].name}: {excerpt or '(no error found in log)'}"
            return False, 'pipeline did not run'

        return True, None

    def _find_aimless_log(self, index_path):
        """Given an index_X_Y folder, return (aimless_log, pipeline_subdir).

        aimless_log is None if no completed job is found.
        pipeline_subdir is None only for an unresolved force-dir case where
        no subdir succeeded and there is no single candidate to diagnose.
        """
        # Flat case: job completed directly under index_path
        if any(
            d.endswith('_xtriage') and os.path.isdir(os.path.join(index_path, d))
            for d in os.listdir(index_path)
        ):
            logs = []
            for d in os.listdir(index_path):
                if d.endswith('_aimless') and os.path.isdir(os.path.join(index_path, d)):
                    log = os.path.join(index_path, d, f'{d}.log')
                    if os.path.isfile(log):
                        try:
                            logs.append((int(d.split('_')[0]), log))
                        except ValueError:
                            pass
            aimless_log = max(logs, key=lambda x: x[0])[1] if logs else None
            return aimless_log, index_path

        # Force dir case: find *_force subdirs, sorted by integer prefix
        # (prefix encodes symmetry order: 1 = highest symmetry)
        force_dirs = sorted(
            [d for d in os.listdir(index_path)
             if d.endswith('_force') and os.path.isdir(os.path.join(index_path, d))],
            key=lambda d: int(d.split('_')[0])
        )
        for force_dir in force_dirs:
            force_path = os.path.join(index_path, force_dir)
            if any(
                d.endswith('_xtriage') and os.path.isdir(os.path.join(force_path, d))
                for d in os.listdir(force_path)
            ):
                # Find the highest-numbered aimless log in this force dir
                aimless_logs = sorted(
                    [(int(d.split('_')[0]), os.path.join(force_path, d, f'{d}.log'))
                     for d in os.listdir(force_path)
                     if d.endswith('_aimless') and os.path.isdir(os.path.join(force_path, d))],
                    key=lambda x: x[0],
                )
                log = aimless_logs[-1][1] if aimless_logs else None
                return (log if log and os.path.isfile(log) else None), force_path

        # No completed job found; flat case can still be diagnosed, force case cannot
        return None, (None if force_dirs else index_path)

    def find_results(self):
        print(f"Looking in {self.result_path}...")
        results = []
        for dataset_name in sorted(os.listdir(self.result_path)):
            dataset_path = os.path.join(self.result_path, dataset_name)
            if not os.path.isdir(dataset_path):
                continue
            parts = dataset_name.split('_')
            if len(parts) < 3:
                continue
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
                    aimless_log, pipeline_subdir = self._find_aimless_log(index_path)
                    results.append({
                        'pin_code'        : pin_code,
                        'dataset_code'    : dataset_code,
                        'proc_path'       : dataset_path,
                        'result_path'     : result_type_path,
                        'aimless_log'     : aimless_log,
                        'pipeline_subdir' : pipeline_subdir,
                        'outcome'         : 'unprocessed',
                    })
        return results

    def scrape_one_log(self, info_dict):
        logfile          = info_dict.get('aimless_log')
        pipeline_subdir  = info_dict.get('pipeline_subdir')

        if not logfile or not os.path.isfile(logfile):
            if pipeline_subdir:
                _, error = self._diagnose(pipeline_subdir)
                if error:
                    info_dict['error'] = error
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
                    single_params[key] = c.replace(key, '').replace(" ", "").strip().lstrip(':')
            for key, value in multi_params.items():
                if key in c:
                    splits = [sp for sp in c.strip().split(' ') if sp != '' and (sp.strip().isdigit() or sp.replace('.','',1).isdigit())]
                    multi_params[key] = splits
        info_dict.update(single_params)
        info_dict.update(multi_params)

        if pipeline_subdir:
            ok, error = self._diagnose(pipeline_subdir)
            if not ok:
                info_dict['error'] = error
                return None

        return info_dict

    def print_info(self):
        HEADERS = ('Dataset', 'Type', 'SG', 'Cell', 'Res', 'Comp', 'Mult', 'CC1/2', 'Rpim', 'Note')
        tagged_rows = []
        for result in self.results:
            code  = result['dataset_code']
            rtype = os.path.basename(result['result_path'])
            if result['outcome'] == 'completed':
                try:
                    comp  = f"{result['Completeness'][0]} ({result['Completeness'][-1]})"
                    mult  = f"{result['Multiplicity'][0]} ({result['Multiplicity'][-1]})"
                    cc12  = f"{result['Mn(I) half-set correlation CC(1/2)'][0]} ({result['Mn(I) half-set correlation CC(1/2)'][-1]})"
                    rpim  = f"{result['Rpim (within I+/I-)'][0]} ({result['Rpim (within I+/I-)'][-1]})"
                    cell  = ' '.join(result['Average unit cell'])
                    res   = result['High resolution limit'][-1]
                    sg    = result['Space group']
                    tagged_rows.append((False, (code, rtype, sg, cell, res, comp, mult, cc12, rpim, '')))
                except (KeyError, TypeError):
                    tagged_rows.append((False, (code, rtype, '?', '?', '?', '?', '?', '?', '?', 'missing stats')))
            else:
                error = result.get('error', '')
                tagged_rows.append((True, (code, rtype, error)))
        self._print_table_mixed(tagged_rows, HEADERS)

    def write_qrefine_list(self, path=None):
        """Write a qrefine_list.txt from the current results list.

        Each completed result is converted back to the
        pin,crystal_name,cell_refined,index,integration format consumed by
        QRefineWriter and qrefine_pipeline.py.  Skips failed/unprocessed results.
        """
        out = pathlib.Path(path) if path else pathlib.Path(os.curdir) / 'qrefine_list.txt'
        lines = []
        for r in self.results:
            if r.get('outcome') != 'completed' or not r.get('pipeline_subdir'):
                continue
            pin         = os.path.basename(r['proc_path'])
            crystal_dir  = os.path.basename(r['result_path'])
            crystal_name = crystal_dir
            cell_refined = ''
            for suffix in _CELL_REFINED_SUFFIXES:
                if crystal_dir.endswith(suffix):
                    crystal_name = crystal_dir[:-len(suffix)]
                    cell_refined = suffix[1:]
                    break
            try:
                rel_parts = (pathlib.Path(r['pipeline_subdir'])
                             .relative_to(r['result_path']).parts)
            except ValueError:
                continue
            if len(rel_parts) == 1:
                index_dir   = rel_parts[0]
                integration = 'integration not forced'
            elif len(rel_parts) == 2:
                index_dir  = rel_parts[0]
                force_base = rel_parts[1][:-len('_force')]
                prefix, sg = force_base.split('_', 1)
                integration = f'integration forced in {sg} {prefix}'
            else:
                continue
            if not index_dir.startswith('index_'):
                continue
            index = index_dir[len('index_'):].replace('_', ' ')
            lines.append(f"{pin},{crystal_name},{cell_refined},{index},{integration}\n")
        with open(out, 'w') as fh:
            fh.writelines(lines)
        print(f"Wrote {len(lines)} entries to {out}")


class QRefineScraper(ScraperBase):
    def _parse_pdb_stats(self, pdb_path):
        """Extract Rwork, Rfree, and resolution from REMARK 3 in a PDB file."""
        rwork = rfree = res = None
        try:
            for line in pathlib.Path(pdb_path).read_text().splitlines():
                if 'R VALUE            (WORKING SET) :' in line:
                    try:
                        rwork = float(line.split(':')[-1].strip())
                    except ValueError:
                        pass
                elif 'FREE R VALUE                     :' in line:
                    try:
                        rfree = float(line.split(':')[-1].strip())
                    except ValueError:
                        pass
                elif 'RESOLUTION RANGE HIGH (ANGSTROMS) :' in line:
                    try:
                        res = float(line.split(':')[-1].strip())
                    except ValueError:
                        pass
        except OSError:
            pass
        return rwork, rfree, res

    def find_results(self):
        print(f"Looking in {self.result_path}...")
        qrefine_dir = os.path.join(self.result_path, 'qrefine')
        results = []
        if not os.path.isdir(qrefine_dir):
            return results
        for job_dir_name in sorted(os.listdir(qrefine_dir)):
            job_dir = os.path.join(qrefine_dir, job_dir_name)
            if not os.path.isdir(job_dir):
                continue
            results.append({
                'job_dir'      : job_dir,
                'job_dir_name' : job_dir_name,
                'outcome'      : 'unprocessed',
            })
        return results

    def scrape_one_log(self, info_dict):
        job_dir   = info_dict['job_dir']
        hoh_pdb   = os.path.join(job_dir, 'final_hoh.pdb')
        nohoh_pdb = os.path.join(job_dir, 'final_nohoh.pdb')

        hoh_stats   = self._parse_pdb_stats(hoh_pdb)   if os.path.isfile(hoh_pdb)   else None
        nohoh_stats = self._parse_pdb_stats(nohoh_pdb) if os.path.isfile(nohoh_pdb) else None

        if hoh_stats and nohoh_stats:
            h_rfree = hoh_stats[1] if hoh_stats[1] is not None else 1.0
            n_rfree = nohoh_stats[1] if nohoh_stats[1] is not None else 1.0
            rwork, rfree, res = hoh_stats if h_rfree <= n_rfree else nohoh_stats
            info_dict['suffix'] = 'hoh' if h_rfree <= n_rfree else 'nohoh'
        elif hoh_stats:
            rwork, rfree, res = hoh_stats
            info_dict['suffix'] = 'hoh'
        elif nohoh_stats:
            rwork, rfree, res = nohoh_stats
            info_dict['suffix'] = 'nohoh'
        else:
            return None

        s = info_dict['suffix']
        info_dict['rwork']      = rwork
        info_dict['rfree']      = rfree
        info_dict['resolution'] = res
        info_dict['phaser']     = os.path.isfile(os.path.join(job_dir, f'02-phaser_{s}.1.pdb'))
        return info_dict

    def print_info(self):
        HEADERS = ('Job', 'Rwork', 'Rfree', 'Res', 'Phaser', 'Model', 'Note')
        tagged_rows = []
        for result in self.results:
            name = result['job_dir_name']
            if result['outcome'] == 'completed':
                rwork  = f"{result['rwork']:.4f}"       if result['rwork']      is not None else 'n/a'
                rfree  = f"{result['rfree']:.4f}"       if result['rfree']      is not None else 'n/a'
                res    = f"{result['resolution']:.2f}A" if result['resolution'] is not None else 'n/a'
                phaser = 'phaser' if result.get('phaser') else 'no_phaser'
                suffix = result.get('suffix', 'n/a')
                tagged_rows.append((False, (name, rwork, rfree, res, phaser, suffix, '')))
            else:
                tagged_rows.append((True, (name, '')))
        self._print_table_mixed(tagged_rows, HEADERS)


class FilterBase:
    """Base class for pipeline result filters.

    Subclasses must implement filter_quality() and select_best().
    run() orchestrates the full two-step filter: quality rejection,
    then per-folder deduplication (one result per top-level proc_path).
    """

    def filter_quality(self, results):
        """Return the subset of results that meet quality thresholds."""
        raise NotImplementedError

    def select_best(self, group):
        """Given a list of results from the same proc_path, return the single best one."""
        raise NotImplementedError

    def run(self, results):
        """Apply quality filter, group by proc_path, and select the best per group."""
        passed = self.filter_quality(results)
        groups = {}
        for r in passed:
            groups.setdefault(r['proc_path'], []).append(r)
        return [self.select_best(group) for group in groups.values()]


class ProcessingFilter(FilterBase):
    """Filter and deduplicate ProcessingScraper results.

    Quality thresholds (Step 1):
      - overall completeness >= 80 %
      - overall multiplicity >= 3
      - overall CC½ >= 0.70
      - outer-shell CC½ >= 0.30
      - overall Rpim <= 0.20

    Deduplication (Step 3): within each proc_path group, prefer lowest
    overall Rpim; break ties by highest overall CC½; break further ties by
    highest resolution (smallest outer-shell limit); fall back to list order.
    """

    COMPLETENESS_MIN = 80.0
    MULTIPLICITY_MIN = 3.0
    CC12_OVERALL_MIN = 0.70
    CC12_OUTER_MIN   = 0.30
    RPIM_MAX         = 0.20

    def filter_quality(self, results):
        passed = []
        for r in results:
            if r.get('outcome') != 'completed':
                continue
            try:
                comp  = float(r['Completeness'][0])
                mult  = float(r['Multiplicity'][0])
                cc12  = float(r['Mn(I) half-set correlation CC(1/2)'][0])
                cc12o = float(r['Mn(I) half-set correlation CC(1/2)'][-1])
                rpim  = float(r['Rpim (within I+/I-)'][0])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if (comp  >= self.COMPLETENESS_MIN and
                mult  >= self.MULTIPLICITY_MIN  and
                cc12  >= self.CC12_OVERALL_MIN  and
                cc12o >= self.CC12_OUTER_MIN    and
                rpim  <= self.RPIM_MAX):
                passed.append(r)
        return passed

    def select_best(self, group):
        def sort_key(r):
            try:
                rpim = float(r['Rpim (within I+/I-)'][0])
                cc12 = float(r['Mn(I) half-set correlation CC(1/2)'][0])
                res  = float(r['High resolution limit'][-1])
            except (KeyError, TypeError, ValueError, IndexError):
                return (float('inf'), float('-inf'), float('inf'))
            return (rpim, -cc12, res)
        return min(group, key=sort_key)


class RefinementFilter(FilterBase):
    """Filter and deduplicate refinement results. (Not yet implemented.)"""

    def filter_quality(self, results):
        raise NotImplementedError

    def select_best(self, group):
        raise NotImplementedError


class WatcherBase(object):

    _OMNIBUS_LOG = "processing.log"

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
        self._log_offsets = {}
        self.get_info()

    def get_info(self):
        raise NotImplementedError

    @property
    def context_width(self):
        """Max width of the context string written to log lines by this pipeline.

        Subclasses should override when the log context contains more than just
        the dataset key (e.g. QRefineWatcher adds the fixed job_id suffix).
        """
        return max((len(k) for k in self.datasets), default=0)

    def _pipeline_logs(self, *_):
        raise NotImplementedError

    def watch(self):
        raise NotImplementedError

    def _emit(self, line, log_file=None):
        """Print a line and optionally append it to a log file."""
        print(line, end="", flush=True)
        if log_file:
            with open(log_file, "a") as fh:
                fh.write(line)

    def tail_logs(self, log_file=None):
        raise NotImplementedError

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

    def get_counts(self):
        """Return (finished, failed, in_progress, rejected, total) counts."""
        self.watch()
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
        return finished, failed, in_progress, rejected, len(self.datasets)

    def is_running(self):
        """Return True if any jobs are still in progress."""
        finished, failed, in_progress, rejected, total = self.get_counts()
        return in_progress > 0

    def tail_log(self):
        """Print any new lines added to the omnibus log since the last call."""
        omnibus = os.path.join(self.procpath, self._OMNIBUS_LOG)
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
        """Return True if still running. Display is handled by ProgressBar."""
        finished, failed, in_progress, rejected, total = self.get_counts()
        return in_progress > 0

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


class ProcessingWatcher(WatcherBase):

    def get_info(self):
        if not os.path.isdir(self.procpath):
            sys.exit(f"Error: processing directory '{self.procpath}' does not exist.")
        pin_list_path = os.path.join(self.procpath, "pin_list.txt")
        if not os.path.isfile(pin_list_path):
            pin_list_path = os.path.join(os.path.dirname(self.procpath), "pin_list.txt")
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
        index_*/ dirs at the top level and *_force/ dirs one level deeper."""
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
                    last_s, last_m = statuses[-1]
                    self.datasets[pin][psf_name] = (last_s, last_m)

    def tail_logs(self, log_file=None):
        """Print any new lines added to pipeline.log files since last call."""
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
                        self._emit(line, log_file)
                        new_lines.append(line)
                    self._log_offsets[log_path] = len(all_lines)
        return new_lines


class QRefineWatcher(WatcherBase):

    _OMNIBUS_LOG = "qrefine.log"
    _JOB_ID_FIXED = 18  # len(" | {forced_tag:<7} | {suffix:<5}")

    @property
    def context_width(self):
        return max((len(pin) for pin in self.datasets), default=0) + self._JOB_ID_FIXED

    def get_info(self):
        if not os.path.isdir(self.procpath):
            sys.exit(f"Error: processing directory '{self.procpath}' does not exist.")
        qrefine_list_path = os.path.join(self.procpath, 'qrefine_list.txt')
        if not os.path.isfile(qrefine_list_path):
            sys.exit(f"Error: 'qrefine_list.txt' not found in '{self.procpath}'.")
        self.datasets = {}
        with open(qrefine_list_path) as fh:
            for line in fh:
                parts = [p.strip() for p in line.strip().split(',')]
                if len(parts) < 5:
                    continue
                pin, crystal_name, cell_refined, index, integration = (
                    parts[0], parts[1], parts[2], parts[3], parts[4]
                )
                index_dir = index.replace(' ', '_')
                if integration == 'integration not forced':
                    forced = 'not_forced'
                else:
                    iparts = integration.split()
                    forced = f"forced_{iparts[3]}_{iparts[4]}"
                cr = f"_{cell_refined}" if cell_refined else ""
                job_dir_name = f"{pin}_{crystal_name}{cr}_{index_dir}_{forced}"
                if pin not in self.datasets:
                    self.datasets[pin] = {}
                self.datasets[pin][job_dir_name] = (None, None)

    def _pipeline_logs(self, job_dir):
        log = os.path.join(job_dir, 'pipeline.log')
        return [log] if os.path.isfile(log) else []

    def watch(self):
        qrefine_dir = os.path.join(self.procpath, 'qrefine')
        for pin in self.datasets:
            for job_dir_name in self.datasets[pin]:
                job_dir = os.path.join(qrefine_dir, job_dir_name)
                logs = self._pipeline_logs(job_dir)
                if not logs:
                    continue
                statuses = [self._read_pipeline_log(l) for l in logs]
                statuses = [(s, m) for s, m in statuses if s is not None]
                if not statuses:
                    continue
                if any(s == "FAILED" for s, _ in statuses):
                    failed_msg = next(m for s, m in statuses if s == "FAILED")
                    self.datasets[pin][job_dir_name] = ("FAILED", failed_msg)
                elif all(s == "FINISHED" for s, _ in statuses):
                    self.datasets[pin][job_dir_name] = ("FINISHED", None)
                else:
                    last_s, last_m = statuses[-1]
                    self.datasets[pin][job_dir_name] = (last_s, last_m)

    def tail_logs(self, log_file=None):
        """Print any new lines added to job pipeline.log files since last call."""
        new_lines = []
        qrefine_dir = os.path.join(self.procpath, 'qrefine')
        for pin in self.datasets:
            for job_dir_name in self.datasets[pin]:
                job_dir = os.path.join(qrefine_dir, job_dir_name)
                for log_path in self._pipeline_logs(job_dir):
                    try:
                        with open(log_path) as fh:
                            all_lines = fh.readlines()
                    except OSError:
                        continue
                    offset = self._log_offsets.get(log_path, 0)
                    for line in all_lines[offset:]:
                        self._emit(line, log_file)
                        new_lines.append(line)
                    self._log_offsets[log_path] = len(all_lines)
        return new_lines


class PanDDAWatcher(WatcherBase):
    """Watches a PanDDA run via processed_datasets/ .log files, masks CSV, and sentinel files."""

    def get_info(self):
        if not os.path.isdir(self.procpath):
            sys.exit(f"Error: pandda output directory '{self.procpath}' does not exist.")
        self._datasets_dir  = pathlib.Path(self.procpath) / 'processed_datasets'
        self._done_file     = pathlib.Path(self.procpath) / 'pandda.done'
        self._masks_csv     = pathlib.Path(self.procpath) / 'analyses' / 'all_datasets_info_masks.csv'
        self._rejected      = set()
        self.datasets       = {}

    def _read_masks(self):
        if not self._masks_csv.is_file():
            return
        try:
            with open(self._masks_csv) as fh:
                header = fh.readline().strip().split(',')
                try:
                    col = header.index('rejected - total')
                except ValueError:
                    return
                for line in fh:
                    parts = line.strip().split(',')
                    if len(parts) > col and parts[col].strip().lower() == 'true':
                        self._rejected.add(parts[0].strip())
        except OSError:
            pass

    def _pipeline_logs(self, *_):
        return []

    def watch(self):
        self._read_masks()
        if not self._datasets_dir.is_dir():
            return
        for entry in sorted(self._datasets_dir.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name in self._rejected:
                self.datasets[name] = ('REJECTED', None)
            elif (entry / f'{name}.log').is_file():
                self.datasets[name] = ('FINISHED', None)
            elif name not in self.datasets:
                self.datasets[name] = ('RUNNING', None)

    def get_counts(self):
        self.watch()
        done        = self._done_file.is_file()
        finished    = sum(1 for s, _ in self.datasets.values() if s == 'FINISHED')
        rejected    = sum(1 for s, _ in self.datasets.values() if s == 'REJECTED')
        total       = len(self.datasets)
        in_progress = 0 if done else 1
        return finished, 0, in_progress, rejected, total

    def show_details(self):
        self.watch()
        lines = [
            f"{name}: {status}" + (f" ({err})" if err else "")
            for name, (status, err) in self.datasets.items()
        ]
        print("\n".join(lines) + "\n*************\n")

    def tail_log(self):
        logs_dir = pathlib.Path(self.procpath) / 'logs'
        logs = sorted(logs_dir.glob('*.log')) if logs_dir.is_dir() else []
        if not logs:
            return []
        log_path = str(logs[-1])
        new_lines = []
        try:
            with open(log_path) as fh:
                all_lines = fh.readlines()
        except OSError:
            return []
        offset = self._log_offsets.get(log_path, 0)
        for line in all_lines[offset:]:
            print(line, end='', flush=True)
            new_lines.append(line)
        self._log_offsets[log_path] = len(all_lines)
        return new_lines

    def tail_logs(self, log_file=None):
        return self.tail_log()


Watcher = ProcessingWatcher


class ProgressBar:
    """Displays an updating progress bar for a watcher.

    Simple mode (default): two-line overwrite display — legend on top, bar below.
    Multiline mode: one line per category (processed / failed / running / rejected),
    each with a fill bar, preceded by a spinner title line. Lines appear and
    disappear as counts change; cursor is repositioned on each redraw using ANSI
    escape codes.
    """

    _SIMPLE_LEGEND = "# processed | ! failed | - running | X rejected"
    _MULTILINE_CATEGORIES = [
        ("finished",  "finished",    "#"),
        ("failed",    "failed",      "!"),
        #("running",   "in_progress", "-"),
        ("rejected",  "rejected",    "X"),
        ("total",     "no_longer_running", "*"),
    ]

    _SPINNER_CHARS  = ["⠉", "⠛", "⠶", "⣤", "⣀", "⣤", "⠶", "⠛"]
    _SPINNER_PERIOD = 0.6  # full cycle duration in seconds

    _PIPELINE_TITLES = {
        "ProcessingWatcher" : lambda path: f"Processing data in {path}",
        "QRefineWatcher"    : lambda path: f"Modeling structures from data in {path}",
        "PanDDAWatcher"     : lambda path: "Running panDDA",
    }

    def __init__(self, watcher, multiline=False, title=None, separator=False):
        self.watcher = watcher
        self.multiline = multiline
        self.separator = separator
        self._drawn = False
        self._prev_lines = 0  # lines printed on last draw (multiline mode)
        if title is not None:
            self._title = title
        else:
            path = getattr(watcher, 'procpath', '')
            make = self._PIPELINE_TITLES.get(type(watcher).__name__)
            self._title = make(path) if make else f"Running pipeline in {path}"

    def _spinner_char(self):
        n   = len(self._SPINNER_CHARS)
        idx = int((time.time() % self._SPINNER_PERIOD) / self._SPINNER_PERIOD * n)
        return self._SPINNER_CHARS[idx % n]

    def erase(self):
        """Clear the bar area so new content can be printed above it.

        Moves the cursor to the top-left of the bar block, clears to end of
        screen, and resets draw state so the next draw() call prints fresh.
        """
        if not self._drawn:
            return
        n_up = (self._prev_lines - 1) if self.multiline else 1
        if n_up > 0:
            print(f"\033[{n_up}A", end="")
        print("\r\033[J", end="", flush=True)
        self._drawn = False
        self._prev_lines = 0

    def draw(self):
        """Redraw progress in place. Returns True if any jobs still running."""
        finished, failed, in_progress, rejected, total = self.watcher.get_counts()
        if self.multiline:
            return self._draw_multiline(in_progress, rejected, finished, failed, total)
        return self._draw_simple(finished, failed, in_progress, rejected, total)

    # ------------------------------------------------------------------
    # Simple two-line mode
    # ------------------------------------------------------------------

    def _draw_simple(self, finished, failed, in_progress, rejected, total):
        bar_width = min(total, 40) if total > 0 else 40
        if total > 0:
            n_failed   = round(failed      / total * bar_width)
            n_not_proc = round(rejected    / total * bar_width)
            n_active   = round(in_progress / total * bar_width)
            if n_active == 0 and in_progress > 0:
                n_active = 1
            n_done = max(0, bar_width - n_failed - n_not_proc - n_active)
        else:
            n_done = n_failed = n_active = n_not_proc = 0

        bar = "#" * n_done + "!" * n_failed + "-" * n_active + "X" * n_not_proc

        if self._drawn:
            print("\033[1A\r", end="")   # up 1 line, back to column 0

        print(f"\033[2K {self._spinner_char()}  {self._SIMPLE_LEGEND}")
        print(f"\033[2K({finished + failed + rejected}/{total}) [{bar}]",
              end="", flush=True)
        self._drawn = True
        return in_progress > 0

    # ------------------------------------------------------------------
    # Multiline per-category mode
    # ------------------------------------------------------------------

    def _draw_multiline(self, in_progress, rejected, finished, failed, total):
        counts = {
            "in_progress":          in_progress,
            "rejected":             rejected,
            "finished":             finished,
            "failed":               failed,
            "total":                total,
            "no_longer_running":    finished + failed + rejected,
        }
        active = [(label, counts[key]) for label, key, _sym in self._MULTILINE_CATEGORIES]

        bar_width = min(total, 50) if total > 0 else 50
        denom = total or 1
        new_lines = len(active) + 1  # +1 for title line
        if self.separator:
            new_lines += 1

        # Reposition cursor to start of our block.
        # Cursor sits at end of last printed line (no trailing \n).
        if self._drawn and self._prev_lines > 0:
            if self._prev_lines > 1:
                print(f"\033[{self._prev_lines - 1}A", end="")
            print("\r", end="")

        # Optional separator between log output above and the bar block.
        # Fixed width avoids wrapping when the terminal is resized at runtime.
        if self.separator:
            print(f"\033[2K{'─' * 72}")

        # Title line with spinner
        print(f"\033[2K {self._spinner_char()}  {self._title}")

        # Draw per-category lines
        for i, (label, count) in enumerate(active):
            n = round(count / denom * bar_width)
            if count > 0 and n == 0:
                bar = "▌"
            else:
                bar = "█" * n
            line = f"\033[2K{label:>10}: {count:>4}/{total:<4} │{bar:<{bar_width}}"
            if i < len(active) - 1:
                print(line)
            else:
                print(line, end="", flush=True)

        # Blank out any extra lines from the previous (longer) draw, then
        # move cursor back up so it sits at the end of the last real line.
        extra = self._prev_lines - new_lines
        if extra > 0:
            for _ in range(extra):
                print()
                print("\033[2K", end="")
            print(f"\033[{extra}A", end="", flush=True)

        self._drawn = True
        self._prev_lines = new_lines
        return in_progress > 0


class Spinner:
    """Bouncing-ball terminal spinner.

    HEIGHT=1 uses braille sub-positions to animate within a single line.
    HEIGHT>1 moves the ball physically across HEIGHT rows.
    The message is pinned to the vertical centre of the spinner area.

    Usage::

        with Spinner(height=3, message="Processing..."):
            do_work()

        # or manually:
        s = Spinner(height=1, message="Working")
        s.start()
        ...
        s.stop()
    """

    _BASE_DELAY = 0.06

    def __init__(self, height=1, message=""):
        self.height  = max(1, height)
        self.message = message
        self._stop   = threading.Event()
        self._thread = None
        self._rows, self._chars, self._delays = self._make_sequence()
        self._msg_row = self.height // 2
        self._pad     = " " * (len(self.message) + 2)

    def _make_sequence(self):
        h = self.height
        if h == 1:
            chars  = ["⠉", "⠛", "⠶", "⣤", "⣀", "⣤", "⠶", "⠛"]
            delays = [0.12, 0.07, 0.05, 0.07, 0.12, 0.07, 0.05, 0.07]
            rows   = [0] * len(chars)
        else:
            N    = max(8, 4 * h)
            half = [round((h - 1) * math.sin(math.pi * i / (N - 1))) for i in range(N)]
            rows = half + half[-2:0:-1]
            def _char(pos):
                if pos == 0:     return "⠉"
                if pos == h - 1: return "⣀"
                return "⠶"
            chars  = [_char(r) for r in rows]
            delays = [self._BASE_DELAY * h] * len(rows)
        return rows, chars, delays

    def _draw(self, row, char):
        lines = []
        for r in range(self.height):
            ball = f" {char}" if r == row else "  "
            msg  = f"  {self.message}" if r == self._msg_row else self._pad
            lines.append(ball + msg + "\n")
        return "".join(lines)

    def _run(self):
        n = len(self._rows)
        sys.stdout.write(self._draw(self._rows[0], self._chars[0]))
        sys.stdout.flush()
        i = 0
        while not self._stop.is_set():
            time.sleep(self._delays[i % n])
            if self._stop.is_set():
                break
            i += 1
            j = i % n
            sys.stdout.write(f"\033[{self.height}A" + self._draw(self._rows[j], self._chars[j]))
            sys.stdout.flush()
        sys.stdout.write(f"\033[{self.height}A" + "   \n" * self.height)
        sys.stdout.flush()

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


class QRefineWriter:
    """
    Scans a completed XDS pipeline output directory, then writes
    qrefine_list.txt and qrefine_input_parameters.txt.

    qrefine_list.txt             — one row per completed job (MTZ file found)
    qrefine_input_parameters.txt — one ## crystal ## block per unique
                                   crystal_name; mr_pdb is a placeholder.
    """

    _DEFAULT_PARAMS = [
        ('mr_pdb',          '/path/to/reference.pdb'),
        ('mr_cutoff',       '0.45'),
        ('mr_water',        'both'),
        ('mr_tncs',         'false'),
        ('mr_copies',       '1'),
        ('mr_mw',           '100000'),
        ('mr_seq_id',       '1.0'),
        ('mr_try_monomer',  'false'),
    ]

    def __init__(self, process_dir):
        self.process_dir = pathlib.Path(process_dir).resolve()

    def _find_completed(self):
        entries = []
        print (self.process_dir)

        for mtz in sorted(self.process_dir.rglob('XDS_ASCII_ctruncate.mtz')):
            rel = mtz.relative_to(self.process_dir).parts
            if len(rel) == 6 and rel[0] == 'processing' and rel[4].endswith('_rfree'):
                pin, crystal_dir, index_dir = rel[1], rel[2], rel[3]
                integration = 'integration not forced'
            elif len(rel) == 7 and rel[0] == 'processing' and rel[4].endswith('_force') and rel[5].endswith('_rfree'):
                pin, crystal_dir, index_dir = rel[1], rel[2], rel[3]
                force_base = rel[4][:-len('_force')]          # e.g. '1_P43212'
                prefix, sg = force_base.split('_', 1)
                integration = f'integration forced in {sg} {prefix}'
            else:
                continue

            crystal_name = crystal_dir
            cell_refined = ''
            for suffix in _CELL_REFINED_SUFFIXES:
                if crystal_dir.endswith(suffix):
                    crystal_name = crystal_dir[:-len(suffix)]
                    cell_refined = suffix[1:]
                    break

            if not index_dir.startswith('index_'):
                continue
            index = index_dir[len('index_'):].replace('_', ' ')

            entries.append((pin, crystal_name, cell_refined, index, integration))

        return entries

    def write_qrefine_list(self, path=None):
        print(f"Scanning {self.process_dir} for completed jobs...")

        entries = self._find_completed()

        print(f"Found {len(entries)} completed jobs. Writing qrefine_list.txt...")

        out = pathlib.Path(path) if path else self.process_dir / 'qrefine_list.txt'
        with open(out, 'w') as fh:
            for pin, crystal_name, cell_refined, index, integration in entries:
                fh.write(f"{pin},{crystal_name},{cell_refined},{index},{integration}\n")
        print(str(out))

    def write_qrefine_input_parameters(self, path=None):
        entries = self._find_completed()
        seen = dict.fromkeys(e[1] for e in entries)   # unique crystal_names, insertion order
        out = pathlib.Path(path) if path else self.process_dir / 'qrefine_input_parameters.txt'
        with open(out, 'w') as fh:
            for crystal_name in seen:
                fh.write('## crystal ##\n')
                fh.write(f'crystal_name={crystal_name}\n')
                for key, val in self._DEFAULT_PARAMS:
                    fh.write(f'{key}={val}\n')
        print(str(out))

    def write(self):
        self.write_qrefine_list()
        self.write_qrefine_input_parameters()
