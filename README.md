# AVICENNA - the semi-automated data-to-structure pipeline

## Introduction

AVICENNA is intended to someday become a fully automated data-to-structure pipeline optimized for fragment screening. AVICENNA uses PHIL (Python Hierarchical Input Language) to create modular, multi-step sub-pipelines which can be relatively easily customized, not only by changing individual parameters but by adding, deleting, or moving entire steps. 3 Currently, it processes multiple complete datasets (XDS, Pointless, Aimless), solves and refines each structure (Phaser, Refmac5), then runs electron density analysis software (panDDA). AVICENNA is a set of Python modules, with the separate function (processing, structure solution, analysis) invoked individually, with further plans to connect them all into a contiguous pipeline. Additional plans include expanding the options for backends, such as DIALS, phenix.refine, and panDDA2 (and others) as desired. 

## Installation

Currently, AVICENNA can only be cloned from the Fraser Lab repository by going into an freshly created empty folder and issuing the following commands:

	git clone https://github.com/fraser-lab/avicenna.git
	cd avicenna
	bash vic_install.sh

At the end of the installation, the install script will print out a line to add to your .bashrc / .zshrc file if you want the avicenna environment initialized on startup. Otherwise just issue:

	source $AVICENNA_INSTALL_FOLDER/vic_setup.sh
	
### Updating

Since Avicenna is in constant development at the moment, it's likely that a new feature (or five) has been added every day and your cloned repository needs to be updated. To update, do this:

	cd $AVICENNA_INSTALL_FOLDER
	git pull --rebase

### Running on Wynton

AVICENNA is currently configured only to run on the Wynton computing cluster at UCSF. Because of that, it doesn't need to be launched with a `qsub` command: it issues that command automatically. Future work is intended to make AVICENNA runnable without an available compute cluster, as well.


## Preparing the data for the pipeline

Fragment screening datasets need to be consolidated in one place for AVICENNA to function properly. (This might change eventually, but either way it's good to keep your data tidy.) Also, the data have to be transferred from the beamline drive to either Wynton or the Fraser Lab cluster.

**NOTE**: eventually, AVICENNA should be able to ingest already processed data (i.e. MTZs), which would reduce the amount of downloading needed. It's a work in progress now.

The following scheme is strongly suggested:

1. Create a folder that follows the naming scheme of `<synchrotron>_<date>_transfer`, e.g. `als_20260414_transfer`.
2. Go to that folder: `cd <synchrotron>_<date>_transfer`
3. You can use `scp` to transfer the data, e.g.: `scp -r <username>@<facility.address.gov>:/path/to/data/folders/ .`
 
**NOTE**: Some (most) facilities have very odd data storage schemes, which frequently bury raw data under several layers of other stuff; eventually, AVICENNA will have a function to automatically create an input list and/or re-sort the data into a tidier, more sensible arrangement.

**NOTE**: If you are putting together datasets collected on several days and even at different facilities, you can create a folder named after your project (e.g. `huwe1`) and create multiple transfer folders inside of it. Then give the project folder name to AVICENNA as input (see below) and it will resolve all the subfolders automatically.


## Running AVICENNA sub-pipelines

All AVICENNA sub-pipelines (processing, refinement, and density analysis) run using the same general principle:

	vic.<sub-pipeline> [OPTIONS]

For example:

	vic.process -d /path/to/data/folders
	vic.refine --settings refine.phil space_group=P212121
	vic.analysis --settings analysis.phil

To see the list of options for any given sub-pipeline, issue this command:
	vic.<sub-pipeline> -h

To see the list of default settings (in PHIL format), use the dry-run option, e.g.:
	vic.refine --dry_run

To see the list of default settings (in PHIL format), use the dry-run option, e.g.:
	vic.refine --dry_run

Additional parameters can be added directly via command line, without modifying the PHIL-formatted settings file, e.g.:

	vic.refine --settings refine.phil space_group=P43212 unit_cell=38,38,79,90,90,90

**NOTE**: at this time, refinement and analysis sub-pipelines require additional parameters to be supplied by the user prior to running. These are:

refinement: 
- in `refine.input.reference_pdb`, the path to the reference structure (which will be used to solve and refine each ligand-bound structure)

analysis: 
- the path to the reference structure (PDB) - typically an apo structure obtained from the Protein Data Bank or solved beforehand
- the path to the reference dataset (MTZ) - structure factors and phases for the apo structure
- (optional) the path to the "ground state" datasets, i.e. a set of minimum 30 apo structures and maps, preferably obtained in a separate AVICENNA processing/refinement run and kept in a separate folder


## Viewing the output

To view a short summary of the sub-pipeline output, use the `vic.summary` command with the `--pipeline` option to specify which sub-pipeline's results you wish to view, e.g.:

	vic.summary ./ --p processing 

For a more extensive summary, in the form of a list of per-dataset statistics, add the `--verbose` option.

Each sub-pipeline's output is stored in the current folder by default, under a hard-coded name:
- `processing` 	- processing results (`processing_logs` for the logs)
- `qrefine`		- refinement results (`qrefine_logs` for the logs) 
- `pandda`		- for panDDA results (`pandda_logs` for the logs) 

The `pandda` folder is the one from which `pandda.inspect` should be run from. This folder can be safely copied or moved to a different location, or downloaded to a personal laptop for further analysis.