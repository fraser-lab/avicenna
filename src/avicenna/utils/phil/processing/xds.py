"""XDS indexing/integration backend for the processing sub-pipeline."""

SCOPE_NAME = "xds"

PROGRAMS = {
    "xds": """\
xds
  .short_caption = XDS Parameters
  .help = Parameters for an XDS indexing/integration step.
{
  scenario = *initial_indexing recycle_detector refine_symmetry forced_integration
    .type = choice
    .short_caption = XDS scenario
    .help = Which XDS run mode to use at this step.
    .expert_level = 0
  skip_if_no_improvement = False
    .type = bool
    .short_caption = Skip if no improvement
    .help = For refine_symmetry steps -- skip if the resolution limit has not \
            improved over the prior run.
    .expert_level = 1
  ncpu = 8
    .type = int
    .short_caption = CPUs per job
    .help = Number of CPUs allocated to this XDS SGE job.
    .expert_level = 1
  xds_inp = None
    .type = path
    .short_caption = XDS.INP file
    .help = Path to an existing XDS.INP file -- overrides all other xds.* \
            parameters at this step if provided.
    .expert_level = 0
  detector_edge = 0.8
    .type = float
    .short_caption = Detector edge fraction
    .help = Fraction of the detector radius used as the initial resolution limit.
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
  no_symmetry = False
    .type = bool
    .short_caption = Skip symmetry determination
    .help = If True, skip Pointless space-group determination and use only \
            the XDS-determined symmetry.
    .expert_level = 2
  res_limit = cc12
    .type = str
    .short_caption = Resolution limit criterion
    .help = Criterion for automatic high-resolution cutoff (cc12 or i_over_sigma).
    .expert_level = 1
  cleanup = partial
    .type = str
    .short_caption = Cleanup level
    .help = How much intermediate output to remove after processing \
            (none, partial, or full).
    .expert_level = 2
  refine_cell_integrate = both
    .type = str
    .short_caption = Cell refinement mode
    .help = Whether to refine the unit cell during INTEGRATE (true, false, or both).
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
""",
}
