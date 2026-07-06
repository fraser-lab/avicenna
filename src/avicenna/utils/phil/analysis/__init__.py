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
# Ligand libraries -- single source of truth for both the ligand_library choice
# (built into _HEADER) and resolve_ligand_libraries().
# ---------------------------------------------------------------------------

LIGAND_LIBRARIES = ("DSI_1", "DSI_2", "DSI_3", "EE", "UCSF91_1")
_LIGAND_DEFAULT = "EE"
_LIGAND_CHOICE = " ".join(f"*{lib}" if lib == _LIGAND_DEFAULT else lib
                          for lib in LIGAND_LIBRARIES) + " all"


def resolve_ligand_libraries(selected):
    """Resolve a ``ligand_library`` selection (the list from choice(multi=True)).

    ``"all"`` -- or an empty selection -- expands to every concrete library;
    otherwise the selected libraries in canonical order.  Call this wherever
    ligand libraries are consumed so ``"all"`` reliably overrides any other
    starred choice.
    """
    sel = set(selected or [])
    if not sel or "all" in sel:
        return list(LIGAND_LIBRARIES)
    return [lib for lib in LIGAND_LIBRARIES if lib in sel]

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
    .help = Input settings for the analysis run (apply to any density-analysis \
            program).
  {
    input_datasets = None
      .type = path
      .short_caption = Input datasets
      .help = Path to the density-analysis input datasets (the refinement \
              sub-pipeline output). Required. Pre-populated automatically by \
              vic.refine. Set manually for standalone runs.
      .expert_level = 0
    ground_state_datasets = None
      .type = path
      .short_caption = Ground-state datasets folder
      .help = Optional path to a folder of diffraction datasets collected from \
              ligand-free (apo) crystals. These become the leading PanDDA \
              datasets and define dataset.ground_state_range and dataset.offset. \
              Distinct from reference_pdb/reference_mtz, which are a single \
              reference model.
      .expert_level = 0
    reference_pdb = None
      .type = path
      .short_caption = Reference PDB
      .help = Optional path to a reference structure PDB for pandda.analyse. This \
              is a single, pre-built model -- either pre-existing or generated \
              specifically for the experiment -- NOT one of the ground-state \
              datasets.
      .expert_level = 0
    reference_mtz = None
      .type = path
      .short_caption = Reference MTZ
      .help = Optional path to the reference structure MTZ for pandda.analyse, \
              paired with reference_pdb.
      .expert_level = 0
    ligands = None
      .type = path
      .short_caption = Ligand structures
      .help = Optional path to ligand structures and their CIFs, organised by \
              library and well code.
      .expert_level = 0
    ligand_library = {ligand_choice}
      .type = choice(multi=True)
      .short_caption = Ligand library
      .help = Fragment library or libraries soaked into the crystals. Select one \
              or more. "all" selects every library and overrides any other \
              selection.
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
        _HEADER.replace("{ligand_choice}", _LIGAND_CHOICE)
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
