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
  }
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


def _build_processing_phil() -> str:
    backends = _discover_backends()
    all_programs: dict[str, str] = {}
    for mod in backends:
        all_programs.update(mod.PROGRAMS)
    names = list(all_programs.keys())
    choice = " ".join(f"*{n}" if i == 0 else n for i, n in enumerate(names))
    return (
        _HEADER
        + _STEP_HEADER.replace("{choice}", choice)
        + "".join(all_programs.values())
        + _FOOTER
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

PHIL_DEF = _build_processing_phil()


class ProcessingPHIL(PhilBase):
    """PHIL parameters for the data-processing sub-pipeline (vic.process)."""
    phil_def = PHIL_DEF
