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
	


## Usage

### Processing X-ray diffraction data

AVICENNA is can now be run from a conda environment (activates automatically once you source `vic_setup.sh` as shown above) with aliased commands:

	vic.process [OPTIONS]

The basic options include:

  `-d </path/to/image/files>` - specify path(s) to data files (could be multiple paths)
  
  `-d <file_with_list_of_paths.txt>` - a text file with a list of paths to image folder can also be submitted. **NOTE**: For best results, it's recommended to use absolute paths (e.g. `/path/to/images`) to avoid "file not found" errors.
  
  `-p pin_list.txt` - a text file with a list of pin codes and their corresponding data (see the example below); can be provided instead of the path to image files
  
  `-o </path/to/output/folder>` - use to specify a different output folder than the one from which AVICENNA is run
  
  `-x xds_input_parameters.txt` - a text file with a selection of input parameters for XDS (see example below); not required for basic default scenario (see below), but required if crystal information (space group, unit cell) is to be passed on to XDS, or if some parameters need to be tweaked
  
  `-l` - toggle display of the run log (will eventually be the default)
  
  `--dry_run` - generate `pin_list.txt` and `xds_input_parameters.txt` but do not actually run processing

Whenever `vic_process.py` runs, it outputs its own pin_list.txt and xds_input_parameters.txt, so if you simply wish to generate those files and then edit them later, use the `--dry_run` option.

### Examples

Basic run with defaults and no prior crystal information:
  
  `vic_process.py -d /path/to/image/files/first_sweep/ /path/to/image/files/second_sweep -l`

Run with pre-written `pin_list.txt` and `xds_input_parameters.txt` files (see below for examples)

  `vic_process.py -p pin_list.txt -x xds_input_parameters.txt -l`


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
