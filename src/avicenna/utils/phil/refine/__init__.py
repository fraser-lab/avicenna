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

import freephil

from avicenna.utils.phil._base import PhilBase, _choice_names_from_def, _normalize_choice_case

_PIPELINES_DIR = Path(__file__).parents[3] / "resources" / "pipelines"

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
    reference_mtz = None
      .type = path
      .short_caption = Reference MTZ
      .help = Reference MTZ dataset for the refinement run.
      .expert_level = 0
    reference_pdb = None
      .type = path
      .short_caption = Reference PDB
      .help = Reference coordinate file for molecular replacement.
      .expert_level = 0
  }
  output
    .short_caption = Output
    .help = Where refinement results are written.
  {
    directory = "."
      .type = path
      .short_caption = Output directory
      .help = Root directory for all refinement output.
      .expert_level = 0
    log_folder = None
      .type = path
      .short_caption = Log folder
      .help = Directory for SGE job logs.  Defaults to <directory>/refinement_logs if None.
      .expert_level = 1
  }
  programs
    .short_caption = Software
    .help = Software required for the refinement sub-pipeline.  Checked at \
            CLI invocation before any jobs are submitted.
  {
    ccp4
      .short_caption = CCP4
      .help = CCP4 suite -- used for MTZ manipulation utilities.
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
                the newest installed version.
        .expert_level = 0
    }
    phenix
      .short_caption = Phenix
      .help = Phenix suite -- used for phenix.phaser (MR) and phenix.refine.
    {
      root = None
        .type = path
        .short_caption = Installation root
        .help = Phenix installation root.  Auto-detected from SBGrid or PATH if None.
        .expert_level = 1
      version = latest
        .type = str
        .short_caption = Version
        .help = Phenix version to use.  "latest" selects the newest installed version.
        .expert_level = 0
    }
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
    _resource_file = "default_refine.phil"

    def __init__(self, load_defaults: bool = True):
        self._master = freephil.parse(self.phil_def)
        self._choice_names = _choice_names_from_def(self.phil_def)
        if load_defaults:
            resource = _PIPELINES_DIR / self._resource_file
            self._default_sources = [freephil.parse(resource.read_text())] if resource.exists() else []
        else:
            self._default_sources = []
        self._params = self._master.fetch(sources=self._default_sources).extract()
        self._default_step_ids = [s.id for s in self._params.refine.steps.step]

    def adopt_phil_string(self, phil_string: str):
        phil_string = _normalize_choice_case(phil_string, self._choice_names)
        user_phil = freephil.parse(phil_string)
        merged = self._master.fetch(sources=[*self._default_sources, user_phil]).extract()
        # freephil appends .multiple=True instances rather than merging by id;
        # deduplicate so the last (user) definition of each step id wins,
        # then restore canonical order (defaults first, user-only steps appended).
        steps = merged.refine.steps.step
        seen = {}
        for step in steps:
            seen[step.id] = step
        ordered = [seen.pop(sid) for sid in self._default_step_ids if sid in seen]
        ordered.extend(seen.values())
        merged.refine.steps.step = ordered
        self._params = merged

    def from_file(self, path: str):
        with open(path) as fh:
            self.adopt_phil_string(fh.read())
