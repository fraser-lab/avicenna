# AVICENNA - the (somewhat) automated data-to-structure pipeline

## Introduction

AVICENNA is intended to someday become a fully automated data-to-structure pipeline optimized for fragment screening. It will process a large number of datasets, run difference Fourier and/or molecular replacement to "solve" each structure, then run fragment screening software such as qFit and panDDA. AVICENNA is a set of Python modules, with the separate function (processing, structure solution, etc.) invoked individually, with further plans to connect them all into a contiguous pipeline.

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


## Processing X-ray diffraction data

To run AVICENNA's X-ray diffraction data processing pipeline, use the command

	vic.process [OPTIONS]

The basic options include:

  `-d </path/to/image/files>` - specify path(s) to data files (could be multiple paths)
  
  `-d <file_with_list_of_paths.txt>` - a text file with a list of paths to image folder can also be submitted. **NOTE**: For best results, it's recommended to use absolute paths (e.g. `/path/to/images`) to avoid "file not found" errors.
  
  `-p pin_list.txt` - a text file with a list of pin codes and their corresponding data (see the example below); can be provided instead of the path to image files
  
  `-o </path/to/output/folder>` - use to specify a different output folder than the one from which AVICENNA is run
  
  `-x xds_input_parameters.txt` - a text file with a selection of input parameters for XDS (see example below); not required for basic default scenario (see below), but required if crystal information (space group, unit cell) is to be passed on to XDS, or if some parameters need to be tweaked
  
  `-l` - toggle display of the run log (will eventually be the default)
  
  `--dry_run` - generate `pin_list.txt` and `xds_input_parameters.txt` but do not actually run processing

Whenever `vic_process.py` runs, it outputs its own `pin_list.txt` and `xds_input_parameters.txt`, so if you simply wish to generate those files and then edit them later, use the `--dry_run` option.

### Examples

Basic run with defaults and no prior crystal information:
  
  `vic_process.py -d /path/to/image/files/first_sweep/ /path/to/image/files/second_sweep`

Run with pre-written `pin_list.txt` and `xds_input_parameters.txt` files (see below for examples)

  `vic_process.py -p pin_list.txt -x xds_input_parameters.txt`


### Sample pin_list.txt

**NOTE**: the file *must* be called `pin_list.txt` at this time.

**NOTE**: the paths in the first column *must* be absolute paths at this time.

	/wynton/group/fraser/als_20251107_transfer,FRS-049_03
	/wynton/group/fraser/als_20251107_transfer,FRS-049_04
	/wynton/group/fraser/als_20251107_transfer,FRS-049_05
	/wynton/group/fraser/als_20251107_transfer,FRS-049_06
	/wynton/group/fraser/als_20251121_transfer,QCR005_01

### Sample xds_input_parameters.txt

**NOTE**: the file *must* be called `xds_input_parameters.txt` at this time

**NOTE**: to process without prior crystal information, remove everything from (and including) `## crystal ##` down.

	script_dir=/wynton/group/fraser/alyubimov/processing_pipeline/scripts
	image_dir=/wynton/group/fraser
	detector_edge=0.8
	indexing_images=first_half
	try_alt_sym=false
	ncpu=8
	no_symmetry=false
	scaling=correct
	recycle_xds=false
	refine_cell_integrate=both
	res_limit=cc12
	cleanup=partial
	## crystal ##
	first_pass=P43212
	sg=P43212
	number=96
	cell=62.97 62.97 231.24 90.00 90.00 90.00
	ref_hkl=none
	ref_mtz=none

### Accessing output
AVICENNA will create a `processing` folder, in which it will store the processing software (`XDS`) output and the queue logs. Two programs can give some information about the results of the processing pipeline's work.

- `vic.watch . -m`  -- issued from the root folder for this AVICENNA run, `vic.watch` will track progress in real time. **NOTE**: to run this (optional) program, open a new terminal and log in through there.
- `vic.scrape .` -- issued from the root folder _after_ the run concludes, `vic.scrape` will scrape the logfiles and show a summary of the results.


## Running the structure solution / refinement pipeline
After the XDS pipeline run concludes, AVICENNA will create two files in the output folder (see samples below):
	qrefine_list.txt
	qrefine_input_parameters.txt

These can be direclty input into the next pipeline. However, `qrefine_input_parameters.txt` contains two placeholder items that have to be filled in manually:

- the path to the reference PDB file - this is the PDB file of the molecule of interest to be used as a starting model for the molecular replacement and/or refinement jobs.
- the molecular weight of the protein (in Daltons) - this has a _huge_ effect on structure solution and refinement algorithms.

After modifying the `qrefine_list.txt` and `qrefine_input_parameters.txt`, run the `vic.refine` program:

	vic.refine -l qrefine_list.txt -p qrefine_input_parameters.txt

You can monitor the in-progress run by opening a new terminal window, logging in, going to the AVICENNA root folder and issuing:

	vic.watch . -m --pipeline qrefine

### Sample `qrefine_list.txt` file

**NOTE**: The file _must_ be called `qrefine_list.txt` at this time.

	als_20251121_QCR005_02_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_02_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_03_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_03_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_04_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_04_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_05_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_05_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_06_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_06_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_07_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_07_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_08_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_08_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_10_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_10_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_11_1,1_P43212,,1 900,integration forced in P43212 1
	als_20251121_QCR005_12_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_12_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_13_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_13_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_14_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_14_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_15_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR005_15_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR005_16_1,1_P43212,,1 900,integration forced in P43212 1
	als_20251121_QCR006_01_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR006_01_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR006_02_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR006_02_1,1_P43212,refined,1 900,integration not forced
	als_20251121_QCR006_16_1,1_P43212,,1 900,integration not forced
	als_20251121_QCR006_16_1,1_P43212,refined,1 900,integration not forced

## Sample `qrefine_input_parameters.txt` file

**NOTE**: the file _must_ be called `qrefine_input_parameters.txt` at this time.

	## crystal ##
	crystal_name=1_P43212
	mr_pdb=/path/to/reference.pdb
	mr_cutoff=0.45
	mr_water=both
	mr_tncs=false
	mr_copies=1
	mr_mw=100000
	mr_seq_id=1.0
	mr_try_monomer=false

## Accessing output
AVICENNA creates a folder called `qrefine` under the root folder. It will have solved / refined structures that can serve as input into panDDA suite. As above, you can use two programs to monitor / analyze the output:

- `vic.watch . -m --pipeline qrefine` - in a separate terminal window, can be used to monitor the run in progress
- `vic.scrape . --pipeline qrefine` - after the run concludes, can be used to see the table of results
