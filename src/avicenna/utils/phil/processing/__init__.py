"""PHIL parameters for the data-processing sub-pipeline (vic.process).

Sub-pipeline plugin interface
------------------------------
Drop a .py file into this directory.  The file must export:

  SCOPE_NAME : str
      Human-readable backend group label.

  PROGRAMS : dict[str, str]
      Maps each program name to its PHIL definition string.  Every
      program becomes an entry in the step ``program`` choice and
      contributes a parameter sub-scope inside the step template.

ProcessingPHIL discovers all backend modules at import time and assembles
the full ``processing { ... }`` PHIL scope automatically.
"""

import importlib
import pkgutil
from pathlib import Path

from avicenna.utils.phil._base import PhilBase

# ---------------------------------------------------------------------------
# Fixed sub-pipeline header (input / output) — not backend-specific
# ---------------------------------------------------------------------------

_HEADER = """\
processing
  .short_caption = Data Processing
  .help = Parameters for the data-processing sub-pipeline.
{
  input
    .short_caption = Input
    .help = Paths to raw data and pre-existing input files.
  {
    data = None
      .type = strings
      .short_caption = Data directories
      .help = One or more paths to raw-data transfer directories \
              (format: <synchrotron>_<YYYYMMDD>_transfer).
      .expert_level = 0
    pinlist = None
      .type = path
      .short_caption = Pin list
      .help = Path to an existing pin_list.txt -- skips data discovery if provided.
      .expert_level = 0
    reference_mtz = None
      .type = path
      .short_caption = Reference MTZ
      .help = Reference MTZ dataset for copying R-free flags to all processed datasets.
      .expert_level = 0
  }
  dataset
    .short_caption = Dataset
    .help = Program-agnostic dataset-processing settings (apply to any indexing program).
  {
    indexing_images = first_half
      .type = str
      .short_caption = Indexing images
      .help = Which images to index (first_half, last_half, all, quarters, eighths, or an explicit range).
      .expert_level = 1
    refine_cell_integrate = *both true false
      .type = choice
      .short_caption = Cell refinement mode
      .help = Refine the unit cell during integration -- both spawns two branches (cell-refined and not).
      .expert_level = 1
    no_symmetry = False
      .type = bool
      .short_caption = Also process without symmetry
      .help = If True, also process each dataset with no symmetry constraint (a free run), in addition to any known_symmetry isoforms.
      .expert_level = 2
    reference_hkl = None
      .type = path
      .short_caption = Reference HKL
      .help = Reference XDS_ASCII.HKL for reindexing / isomorphism-guided integration.
      .expert_level = 1
  }
  output
    .short_caption = Output
    .help = Where processed results are written.
  {
    directory = "."
      .type = path
      .short_caption = Output directory
      .help = Root directory for all processing output.
      .expert_level = 0
    log_folder = None
      .type = path
      .short_caption = Log folder
      .help = Directory for SGE job logs.  Defaults to <directory>/processing_logs if None.
      .expert_level = 1
  }
  known_symmetry
    .short_caption = Known symmetry
    .help = Known crystal isoforms for constrained data processing.
  {
    enabled = False
      .type = bool
      .short_caption = Use known symmetry
      .help = Set to True to apply the isoform definitions below during processing.
      .expert_level = 0
    isoform
      .multiple = True
      .short_caption = Isoform
    {
      id = None
        .type = str
        .short_caption = Isoform ID
        .help = Unique isoform label (e.g. isoform_1).  Required.
        .expert_level = 0
      space_group = None
        .type = str
        .short_caption = Space group
        .help = Space group in international notation (e.g. P41212. P21. C2. \
                R32). Abbreviated names and CCP4 H-prefix rhombohedral names \
                (H32. H3) are accepted. The space group number is derived \
                automatically.
        .expert_level = 0
      unit_cell = None
        .type = floats(size=6)
        .short_caption = Unit cell
        .help = Unit cell constants a b c alpha beta gamma in Angstroms and degrees.
        .expert_level = 0
      reference_hkl = None
        .type = path
        .short_caption = Reference HKL
        .help = Reference XDS_ASCII.HKL for isomorphism-guided integration.
        .expert_level = 1
    }
  }
  programs
    .short_caption = Software
    .help = Crystallographic software to use.  Checked at CLI invocation \
            before any jobs are submitted.
  {
    xds
      .short_caption = XDS
      .help = XDS indexing and integration.  Always uses the latest installed \
              version -- XDS is license-locked per version so prior versions \
              become unusable after renewal.
    {
      root = None
        .type = path
        .short_caption = Installation root
        .help = XDS installation root (must contain bin/xds_par).  \
                Auto-detected from SBGrid or PATH if None.
        .expert_level = 1
    }
    ccp4
      .short_caption = CCP4
      .help = CCP4 suite -- provides Pointless, Aimless, freerflag, ctruncate.
    {
      root = None
        .type = path
        .short_caption = Installation root
        .help = CCP4 installation root.  Auto-detected from SBGrid or PATH if None.
        .expert_level = 1
      version = latest
        .type = str
        .short_caption = Version
        .help = CCP4 version to use (e.g. "9.0", "7.0").  "latest" selects \
                the newest installed version.  Specify "7.0" for original \
                panDDA compatibility.
        .expert_level = 0
    }
    phenix
      .short_caption = Phenix
      .help = Phenix suite -- provides phenix.xtriage.
    {
      root = None
        .type = path
        .short_caption = Installation root
        .help = Phenix installation root.  Auto-detected from SBGrid or PATH if None.
        .expert_level = 1
      version = latest
        .type = str
        .short_caption = Version
        .help = Phenix version to use (e.g. "1.21").  "latest" selects the \
                newest installed version.
        .expert_level = 0
    }
  }
"""

# Steps scope opener — kept separate so program SETTINGS scopes can be inserted
# between the fixed header and the step list in _build_processing_phil.
_STEPS_OPEN = """\
  steps
    .short_caption = Pipeline steps
    .help = Ordered sequence of programs to run.  Edit, reorder, or replace \
            steps to customise the pipeline.  Use --print-pipeline to export \
            the current default as a starting point.
  {
"""

_STEP_HEADER = """\
    step
      .multiple = True
    {
      id = None
        .type = str
        .short_caption = Step ID
        .help = Unique step label (required -- prevents PHIL merging identical entries).
        .expert_level = 0
      program = {choice}
        .type = choice
        .short_caption = Program
        .help = Which program to run at this step.
        .expert_level = 0
      run_if_succeeded = None
        .type = str
        .short_caption = Run if step succeeded
        .help = Step ID that must have completed successfully for this step to run.  Skipped otherwise.
        .expert_level = 1
      run_if_failed = None
        .type = str
        .short_caption = Run if step failed
        .help = Step ID that must have failed or been skipped for this step to run.  Skipped otherwise.
        .expert_level = 1
"""

_FOOTER = """\
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Backend discovery
# ---------------------------------------------------------------------------

def _discover_backends():
    package_dir = Path(__file__).parent
    modules = []
    for _finder, name, _ispkg in pkgutil.iter_modules([str(package_dir)]):
        if name.startswith("_"):
            continue
        mod = importlib.import_module(f"avicenna.utils.phil.processing.{name}")
        if hasattr(mod, "PROGRAMS"):
            modules.append(mod)
    return modules


def _build_processing_phil():
    """Return (phil_def, program_blocks) assembled from the program catalog.

    program_blocks maps each program choice value to its settings-block scope
    name, so the serializer can emit just the one block belonging to each step's
    program.
    """
    all_programs: dict[str, str] = {}
    settings_blocks = []
    for mod in _discover_backends():
        all_programs.update(mod.PROGRAMS)
        if hasattr(mod, "SETTINGS"):
            settings_blocks.append(mod.SETTINGS)
    names = list(all_programs.keys())
    choice = " ".join(f"*{n}" if i == 0 else n for i, n in enumerate(names))
    phil_def = (
        _HEADER
        + "".join(settings_blocks)          # per-program global settings scopes
        + _STEPS_OPEN
        + _STEP_HEADER.replace("{choice}", choice)
        + "".join(all_programs.values())
        + _FOOTER
    )
    program_blocks = {prog: block.split(None, 1)[0] for prog, block in all_programs.items()}
    return phil_def, program_blocks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

PHIL_DEF, PROGRAM_BLOCKS = _build_processing_phil()


class ProcessingPHIL(PhilBase):
    """PHIL parameters for the data-processing sub-pipeline (vic.process)."""
    phil_def = PHIL_DEF
    _resource_file = "default_processing.phil"
    _scope_name = "processing"
    _program_blocks = PROGRAM_BLOCKS
