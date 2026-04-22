"""
PHIL-based parameter system for the Avicenna pipeline.

Classes
-------
PhilBase        -- abstract base: PHIL definition, adopt/export helpers
ProcessingPHIL  -- parameters for the data-processing sub-pipeline (vic.process)
RefinePHIL      -- parameters for the refinement sub-pipeline (vic.refine)
PanDDAPHIL      -- parameters for the panDDA sub-pipeline (vic.pandda)
AvicennaPHIL    -- master scope nesting all sub-pipeline scopes
"""

import freephil

# ---------------------------------------------------------------------------
# Sub-pipeline PHIL definition strings
# ---------------------------------------------------------------------------

_PROCESSING_PHIL = """\
processing
  .short_caption = Data Processing
  .help = Parameters for the XDS/Aimless data-processing sub-pipeline.
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
    xdsparams = None
      .type = path
      .short_caption = XDS parameter file
      .help = Path to an existing xds_input_parameters.txt -- \
              overrides all xds.* parameters below if provided.
      .expert_level = 0
    xds_root = None
      .type = path
      .short_caption = XDS installation root
      .help = Explicit path to the XDS installation directory \
              (containing bin/xds_par). Defaults to the SBGrid install.
      .expert_level = 2
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
  xds
    .short_caption = XDS Parameters
    .help = Parameters forwarded to the XDS processing worker script.
  {
    script_dir = "../scripts"
      .type = str
      .short_caption = Script directory
      .help = Directory containing the XDS pipeline shell scripts.
      .expert_level = 2
    detector_edge = 0.8
      .type = float
      .short_caption = Detector edge fraction
      .help = Fraction of the detector radius used as the resolution limit \
              for initial spot-finding.
      .expert_level = 1
    indexing_images = first_half
      .type = str
      .short_caption = Indexing images
      .help = Which images to use for indexing (first_half, all, or a range).
      .expert_level = 1
    try_alt_sym = all
      .type = str
      .short_caption = Try alternative symmetry
      .help = Whether to attempt alternative space groups after indexing \
              (all, none, or a comma-separated list of lattice types).
      .expert_level = 1
    ncpu = 8
      .type = int
      .short_caption = CPUs per job
      .help = Number of CPUs allocated to each XDS SGE job.
      .expert_level = 1
    no_symmetry = False
      .type = bool
      .short_caption = Skip symmetry determination
      .help = If True, skip Pointless space-group determination and \
              use only the XDS-determined symmetry.
      .expert_level = 2
    scaling = correct
      .type = str
      .short_caption = Scaling stage
      .help = Which program to use for final scaling (correct or aimless).
      .expert_level = 1
    recycle_xds = False
      .type = bool
      .short_caption = Recycle XDS
      .help = If True, re-run XDS CORRECT using the Aimless-determined \
              space group to refine the solution.
      .expert_level = 2
    refine_cell_integrate = both
      .type = str
      .short_caption = Cell refinement mode
      .help = Whether to refine the unit cell during INTEGRATE \
              (true, false, or both).
      .expert_level = 2
    res_limit = cc12
      .type = str
      .short_caption = Resolution limit criterion
      .help = Criterion for automatic high-resolution cutoff \
              (cc12 or i_over_sigma).
      .expert_level = 1
    cleanup = partial
      .type = str
      .short_caption = Cleanup level
      .help = How much intermediate output to remove after processing \
              (none, partial, or full).
      .expert_level = 2
    ref_hkl = None
      .type = path
      .short_caption = Reference HKL
      .help = Path to a reference HKL file for reindexing (optional).
      .expert_level = 2
    ref_mtz = None
      .type = path
      .short_caption = Reference MTZ
      .help = Path to a reference MTZ file for reindexing (optional).
      .expert_level = 2
  }
}
"""

_REFINE_PHIL = """\
refine
  .short_caption = Refinement
  .help = Parameters for the qRefine crystallographic refinement sub-pipeline.
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
}
"""

_PANDDA_PHIL = """\
pandda
  .short_caption = PanDDA
  .help = Parameters for the panDDA event-map sub-pipeline.
{
  input
    .short_caption = Input
    .help = Source directories and reference structures.
  {
    sources = None
      .type = strings
      .short_caption = Source directories
      .help = One or more directories of completed qRefine jobs to \
              collect into the panDDA dataset.
      .expert_level = 0
    reference_pdb = None
      .type = path
      .short_caption = Reference PDB
      .help = Reference PDB file for pandda.analyse.
      .expert_level = 0
    reference_mtz = None
      .type = path
      .short_caption = Reference MTZ
      .help = Reference MTZ file for pandda.analyse.
      .expert_level = 0
  }
  dataset
    .short_caption = Dataset options
    .help = Controls dataset naming and selection.
  {
    prefix = "FRS"
      .type = str
      .short_caption = Dataset prefix
      .help = Prefix applied to panDDA dataset names (e.g. FRS -> FRS-x0001).
      .expert_level = 0
    offset = 0
      .type = int
      .short_caption = Numbering offset
      .help = Integer offset applied to dataset serial numbers.
      .expert_level = 1
    suffix = *hoh nohoh
      .type = choice
      .short_caption = qRefine output variant
      .help = Which qRefine output to use: with solvent (hoh) or without (nohoh).
      .expert_level = 1
    ground_state_range = None
      .type = str
      .short_caption = Ground-state range
      .help = Dataset number range to treat as ground state, e.g. "1-44".
      .expert_level = 0
  }
  analyse
    .short_caption = pandda.analyse options
    .help = Resource and quality parameters passed to pandda.analyse.
  {
    cpus = 32
      .type = int
      .short_caption = CPUs
      .help = Number of CPUs for pandda.analyse.
      .expert_level = 1
    min_build_datasets = 30
      .type = int
      .short_caption = Minimum build datasets
      .help = Minimum number of datasets required before model building begins.
      .expert_level = 1
    max_build_datasets = 50
      .type = int
      .short_caption = Maximum build datasets
      .help = Maximum number of datasets used for the ground-state model.
      .expert_level = 1
    max_new_datasets = 1500
      .type = int
      .short_caption = Maximum new datasets
      .help = Maximum number of new (non-ground-state) datasets to analyse.
      .expert_level = 1
    no_lr_comp_check = False
      .type = bool
      .short_caption = Skip low-res completeness check
      .help = Disable the low-resolution completeness check in pandda.analyse.
      .expert_level = 2
  }
  run
    .short_caption = Run options
    .help = Controls over how the panDDA job is launched.
  {
    log_time_out = 0
      .type = int
      .short_caption = Log timeout (minutes)
      .help = Stop waiting for the pandda log after this many minutes \
              (0 = no timeout).
      .expert_level = 1
  }
}
"""

_AVICENNA_PHIL = f"""\
avicenna
  .short_caption = Avicenna
  .help = Master scope for the full Avicenna pipeline.
{{
  project
    .short_caption = Project
    .help = Top-level project metadata shared across sub-pipelines.
  {{
    name = None
      .type = str
      .short_caption = Project name
      .help = Human-readable label for this processing campaign.
      .expert_level = 0
    root_dir = "."
      .type = path
      .short_caption = Project root directory
      .help = Root directory for all sub-pipeline output.
      .expert_level = 0
  }}
{_PROCESSING_PHIL}{_REFINE_PHIL}{_PANDDA_PHIL}}}
"""


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class PhilBase:
    """Abstract base for all Avicenna PHIL parameter objects.

    Subclasses set ``phil_def`` (a PHIL definition string) at the class level.
    Instances hold a ``params`` scope_extract built from that definition.
    """

    phil_def: str = ""

    def __init__(self):
        self._master = freephil.parse(self.phil_def)
        self._params = self._master.fetch().extract()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def master(self):
        """The freephil master scope (definition object)."""
        return self._master

    @property
    def params(self):
        """The adopted scope_extract (parameter values)."""
        return self._params

    # ------------------------------------------------------------------
    # Adopt
    # ------------------------------------------------------------------

    def adopt_phil_string(self, phil_string: str):
        """Merge a PHIL-formatted string into the current params."""
        user_phil = freephil.parse(phil_string)
        self._params = self._master.fetch(sources=[user_phil]).extract()

    def from_file(self, path: str):
        """Read a .phil file and merge it into the current params."""
        with open(path) as fh:
            self.adopt_phil_string(fh.read())

    def from_dict(self, d: dict, scope: str = ""):
        """Adopt a flat key->value dict as PHIL overrides.

        *scope* is prepended to each key so callers can target a nested scope,
        e.g. ``scope="processing.xds"`` turns ``{"ncpu": 16}`` into the
        PHIL line ``processing.xds.ncpu = 16``.
        """
        prefix = f"{scope}." if scope else ""
        lines = []
        for k, v in d.items():
            if v is None:
                continue
            if isinstance(v, bool):
                lines.append(f"{prefix}{k} = {str(v).upper()}")
            elif isinstance(v, str):
                lines.append(f'{prefix}{k} = "{v}"')
            else:
                lines.append(f"{prefix}{k} = {v}")
        if lines:
            self.adopt_phil_string("\n".join(lines))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def to_file(self, path: str):
        """Write the full (all-params) PHIL to *path*."""
        with open(path, "w") as fh:
            fh.write(self.as_phil_string())

    def as_phil_string(self) -> str:
        """Return the current params as a PHIL-formatted string."""
        return self._master.format(self._params).as_str()

    def diff_phil_string(self) -> str:
        """Return only parameters that differ from their defaults."""
        diff = self._master.fetch_diff(
            sources=[freephil.parse(self.as_phil_string())],
        )
        return diff.as_str()

    def show(self, attributes_level: int = 0):
        """Print the current params (attributes_level controls annotation verbosity)."""
        self._master.format(self._params).show(attributes_level=attributes_level)


# ---------------------------------------------------------------------------
# Sub-pipeline PHIL classes
# ---------------------------------------------------------------------------

class ProcessingPHIL(PhilBase):
    """PHIL parameters for the data-processing sub-pipeline (vic.process)."""
    phil_def = _PROCESSING_PHIL


class RefinePHIL(PhilBase):
    """PHIL parameters for the refinement sub-pipeline (vic.refine)."""
    phil_def = _REFINE_PHIL


class PanDDAPHIL(PhilBase):
    """PHIL parameters for the panDDA sub-pipeline (vic.pandda)."""
    phil_def = _PANDDA_PHIL


# ---------------------------------------------------------------------------
# Master PHIL
# ---------------------------------------------------------------------------

class AvicennaPHIL(PhilBase):
    """Master PHIL scope nesting all sub-pipeline scopes.

    Downstream code that needs to pass parameters across sub-pipeline
    boundaries should accept or produce an AvicennaPHIL instance and
    read from e.g. ``phil.params.avicenna.processing.xds.ncpu``.
    """
    phil_def = _AVICENNA_PHIL
