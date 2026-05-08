"""PHIL parameters for the crystallographic refinement sub-pipeline (vic.refine).

Sub-pipeline plugin interface
------------------------------
Drop a .py file into this directory exporting SCOPE_NAME and PROGRAMS
(same contract as the processing sub-pipeline).  The new backend's program
names are added to the step choice automatically.
"""

import importlib
import pkgutil
from pathlib import Path

from avicenna.utils.phil._base import PhilBase

# ---------------------------------------------------------------------------
# Fixed sub-pipeline header
# ---------------------------------------------------------------------------

_HEADER = """\
refine
  .short_caption = Refinement
  .help = Parameters for the crystallographic refinement sub-pipeline.
{
  input
    .short_caption = Input
    .help = Input files for the refinement run.
  {
    output_dir = "."
      .type = path
      .short_caption = Processing directory
      .help = Directory that contains completed processing output \
              (and will receive refinement output).
      .expert_level = 0
    dataset_list = "qrefine_list.txt"
      .type = path
      .short_caption = Dataset list
      .help = File listing datasets to refine (one per line).
      .expert_level = 0
    params_file = "qrefine_input_parameters.txt"
      .type = path
      .short_caption = MR parameter file
      .help = File containing qRefine/MR parameters.
      .expert_level = 0
  }
  steps
    .short_caption = Pipeline steps
    .help = Ordered sequence of refinement programs to run.
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
        mod = importlib.import_module(f"avicenna.utils.phil.refine.{name}")
        if hasattr(mod, "PROGRAMS"):
            modules.append(mod)
    return modules


def _build_refine_phil() -> str:
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

PHIL_DEF = _build_refine_phil()


class RefinePHIL(PhilBase):
    """PHIL parameters for the refinement sub-pipeline (vic.refine)."""
    phil_def = PHIL_DEF
