"""PanDDA event-map analysis backend for the analysis sub-pipeline."""

SCOPE_NAME = "pandda"

PROGRAMS = {
    "pandda": """\
pandda
  .short_caption = PanDDA Parameters
  .help = Parameters for the PanDDA event-map analysis step.
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
    .help = Controls how the panDDA job is launched.
  {
    log_time_out = 0
      .type = int
      .short_caption = Log timeout (minutes)
      .help = Stop waiting for the pandda log after this many minutes \
              (0 = no timeout).
      .expert_level = 1
  }
}
""",
}
