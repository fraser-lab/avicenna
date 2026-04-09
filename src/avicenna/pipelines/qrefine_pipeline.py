import argparse
import os
import pathlib

parser = argparse.ArgumentParser(prog="vic.refine")
parser.add_argument("-d", "--data", type=pathlib.Path,
                    help="Reflection data file (MTZ)")
parser.add_argument("-m", "--model", type=pathlib.Path,
                    help="Starting model (PDB)")
parser.add_argument("-o", "--output", type=pathlib.Path,
                    default=pathlib.Path(os.curdir),
                    help="Processing directory (default: current directory)")
parser.add_argument("-l", "--list", type=pathlib.Path,
                    default=pathlib.Path("qrefine_list.txt"),
                    help="Dataset list file (default: qrefine_list.txt)")
parser.add_argument("-p", "--params", type=pathlib.Path,
                    default=pathlib.Path("qrefine_input_parameters.txt"),
                    help="MR parameter file (default: qrefine_input_parameters.txt)")
parser.add_argument("--dry_run", action="store_true",
                    help="Write scripts without submitting to SGE")


class QRefineParams:
    """Parse qrefine_input_parameters.txt into per-crystal MR parameter dicts.

    The file is delimited by '## crystal ##' blocks, each containing key=value
    pairs. Provides a .get(crystal_name) lookup.
    """

    def __init__(self, params_path):
        self.params_path = pathlib.Path(params_path)
        self._crystals = {}  # crystal_name -> dict of params

    def parse(self):
        pass

    def get(self, crystal_name):
        pass


class QRefineDataset:
    """Represent one entry from qrefine_list.txt.

    Holds pin, crystal_name, cell_refined, index, and integration flag.
    Derives the input MTZ path (forced vs. non-forced) and the output
    subdirectory name under qrefine/.
    """

    def __init__(self, pin, crystal_name, cell_refined, index, integration, process_dir):
        self.pin = pin
        self.crystal_name = crystal_name
        self.cell_refined = cell_refined
        self.index = index
        self.integration = integration
        self.process_dir = pathlib.Path(process_dir)
        self.forced = None       # str: "not_forced" or "forced_<SG>_<prefix>"
        self.mtz_path = None     # pathlib.Path to input MTZ
        self.job_dir = None      # pathlib.Path: qrefine/<subdir>

    def resolve(self):
        pass


class QRefineWorker:
    """Generate qrefine_qsub_{hoh,nohoh}.sh worker scripts for one dataset.

    Given a QRefineDataset and its MR parameter dict, writes the multi-stage
    refinement pipeline into each worker script:
      1. PDB preparation (strip or rename HOH)
      2. Phenix rigid body refinement
      3. Phaser MR (conditional on R-factor > cutoff)
      4. REFMAC jelly body refinement
      5. REFMAC restrained refinement
      6. Optional monomer retry (Phaser + REFMAC) if multi-copy Phaser failed
         or final Rfree > 0.4
    Returns a list of paths to the generated scripts.
    """

    def __init__(self, dataset, mr_params):
        self.dataset = dataset    # QRefineDataset
        self.mr_params = mr_params

    def write(self):
        pass


class RunQRefine:
    """Orchestrate the full qrefine preparation and SGE submission.

    Reads qrefine_list.txt and qrefine_input_parameters.txt, manages the
    qrefine/ output directory, iterates over datasets, calls QRefineWorker
    for each, writes qrefine_qsub_list.txt and qrefine_qsub_submit.sh,
    and submits via qsub (unless dry_run=True).

    Also ensures CCP4 7.0 is pinned in ~/.sbgrid.conf before job generation.
    """

    def __init__(self, process_dir, dataset_list, params_file, data=None, model=None, dry_run=False):
        self.process_dir = pathlib.Path(process_dir)
        self.dataset_list = pathlib.Path(dataset_list)
        self.params_file = pathlib.Path(params_file)
        self.data = pathlib.Path(data) if data else None
        self.model = pathlib.Path(model) if model else None
        self.dry_run = dry_run
        self.job_scripts = []    # accumulated paths from QRefineWorker.write()

    def _pin_ccp4_version(self):
        pass

    def _setup_output_dir(self):
        pass

    def _read_dataset_list(self):
        pass

    def _write_qsub_submit(self):
        pass

    def run(self):
        pass


def main():
    args = parser.parse_args()
    runner = RunQRefine(
        process_dir=args.output,
        dataset_list=args.list,
        params_file=args.params,
        data=args.data,
        model=args.model,
        dry_run=args.dry_run,
    )
    runner.run()


if __name__ == "__main__":
    main()
