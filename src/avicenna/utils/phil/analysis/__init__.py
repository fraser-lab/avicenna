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


def _build_analysis_phil() -> str:
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

PHIL_DEF = _build_analysis_phil()


class AnalysisPHIL(PhilBase):
    """PHIL parameters for the analysis sub-pipeline (vic.pandda)."""
    phil_def = PHIL_DEF


# Backward-compatible alias
PanDDAPHIL = AnalysisPHIL
