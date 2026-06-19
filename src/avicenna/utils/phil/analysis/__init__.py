"""PHIL parameters for the data-analysis sub-pipeline (vic.pandda).

Sub-pipeline plugin interface
------------------------------
Drop a .py file into this directory exporting SCOPE_NAME and PROGRAMS
(same contract as the processing sub-pipeline).
"""

import importlib
import pkgutil
from pathlib import Path

from avicenna.utils.phil._base import PhilBase

# ---------------------------------------------------------------------------
# Fixed sub-pipeline header
# ---------------------------------------------------------------------------

_HEADER = """\
analysis
  .short_caption = Analysis
  .help = Parameters for the data-analysis sub-pipeline.
{
  input
    .short_caption = Input
    .help = Input files for the analysis run.
  {
    data_directory = None
      .type = path
      .short_caption = Processed data directory
      .help = Directory containing processed MTZ files from vic.process.
      .expert_level = 0
  }
  output
    .short_caption = Output
    .help = Where analysis results are written.
  {
    directory = "."
      .type = path
      .short_caption = Output directory
      .help = Root directory for all analysis output.
      .expert_level = 0
    log_folder = None
      .type = path
      .short_caption = Log folder
      .help = Directory for SGE job logs.  Defaults to <directory>/analysis_logs if None.
      .expert_level = 1
  }
  programs
    .short_caption = Software
    .help = Software required for the analysis sub-pipeline.  Checked at \
            CLI invocation before any jobs are submitted.
  {
    pandda
      .short_caption = PanDDA 1
      .help = PanDDA 1 (pandda.analyse), bundled with CCP4 7.0.  Auto-detected from PATH.
    {
      root = None
        .type = path
        .short_caption = Installation root
        .help = PanDDA 1 installation root.  Auto-detected from PATH if None.
        .expert_level = 1
    }
    pandda2
      .short_caption = PanDDA 2
      .help = PanDDA 2 (pandda2.analyse), installed independently of CCP4.  Auto-detected from PATH.
    {
      root = None
        .type = path
        .short_caption = Installation root
        .help = PanDDA 2 installation root.  Auto-detected from PATH if None.
        .expert_level = 1
    }
  }
  steps
    .short_caption = Pipeline steps
    .help = Ordered sequence of analysis programs to run.
  {
"""

_STEP_HEADER = """\
    step
      .multiple = True
    {
      id = None
        .type = str
        .short_caption = Step ID
        .help = Unique step label (required).
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
        mod = importlib.import_module(f"avicenna.utils.phil.analysis.{name}")
        if hasattr(mod, "PROGRAMS"):
            modules.append(mod)
    return modules


def _build_analysis_phil():
    """Return (phil_def, program_blocks) assembled from the program catalog.

    program_blocks maps each program choice value to its settings-block scope
    name, so the serializer can emit just the one block belonging to each step's
    program.
    """
    all_programs: dict[str, str] = {}
    for mod in _discover_backends():
        all_programs.update(mod.PROGRAMS)
    names = list(all_programs.keys())
    choice = " ".join(f"*{n}" if i == 0 else n for i, n in enumerate(names))
    phil_def = (
        _HEADER
        + _STEP_HEADER.replace("{choice}", choice)
        + "".join(all_programs.values())
        + _FOOTER
    )
    program_blocks = {prog: block.split(None, 1)[0] for prog, block in all_programs.items()}
    return phil_def, program_blocks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

PHIL_DEF, PROGRAM_BLOCKS = _build_analysis_phil()


class AnalysisPHIL(PhilBase):
    """PHIL parameters for the analysis sub-pipeline (vic.analysis)."""
    phil_def = PHIL_DEF
    _resource_file = "default_analysis.phil"
    _scope_name = "analysis"
    _program_blocks = PROGRAM_BLOCKS


# Backward-compatible alias
PanDDAPHIL = AnalysisPHIL
