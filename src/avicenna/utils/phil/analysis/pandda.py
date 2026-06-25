"""PanDDA event-map analysis backend for the analysis sub-pipeline.

Supports pandda.analyse (PanDDA 1, bundled with CCP4 7.0) and
pandda2.analyse (PanDDA 2, standalone install).  Select with the
``analyser`` parameter in the step scope.
"""

SCOPE_NAME = "pandda"

PROGRAMS = {
    "pandda": """\
pandda
  .short_caption = PanDDA Parameters
  .help = Parameters for the PanDDA event-map analysis step.
{
  analyser = *pandda pandda2
    .type = choice
    .short_caption = PanDDA version
    .help = Which PanDDA program to run: pandda (pandda.analyse, requires CCP4 7.0) or pandda2 (pandda2.analyse, standalone install).
    .expert_level = 0
  input
    .short_caption = Input
    .help = Source directories and reference structures.
  {
    sources = None
      .type = strings
      .short_caption = Source directories
      .help = One or more directories of completed qRefine jobs to collect into the panDDA dataset.
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
      .help = Prefix applied to panDDA dataset names (e.g. FRS -> FRS_x0001).
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
    .help = Resource and quality parameters passed to pandda.analyse or pandda2.analyse.
  {
    cpus = auto
      .type = int
      .short_caption = CPUs
      .help = CPUs for pandda.analyse. auto requests one core per dataset (PanDDA \
              parallelises across datasets), capped. An explicit integer is used \
              verbatim.
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
      .help = Disable the low-resolution completeness check.
      .expert_level = 2
  }
  run
    .short_caption = Run options
    .help = Controls how the panDDA job is launched.
  {
    log_time_out = 0
      .type = int
      .short_caption = Log timeout (minutes)
      .help = Stop waiting for the pandda log after this many minutes (0 = no timeout).
      .expert_level = 1
    dry_run = False
      .type = bool
      .short_caption = Dry run
      .help = Prepare directories and write scripts without running pandda.
      .expert_level = 1
  }
}
""",
}


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

def _run_pandda(step, ctx):
    """Run pandda.analyse via PanDDAPreparer."""
    from avicenna.pipelines.legacy.pandda_pipeline import PanDDAPreparer

    p = step.pandda
    sources = list(p.input.sources or [])
    if not sources:
        return {"status": "error", "fatal": True,
                "message": "input.sources is empty"}

    ground_state = []
    if p.dataset.ground_state_range:
        try:
            lo, hi = (int(x) for x in p.dataset.ground_state_range.split("-"))
            ground_state = list(range(lo, hi + 1))
        except (ValueError, AttributeError):
            pass

    preparer = PanDDAPreparer(
        process_dirs=sources,
        prefix=p.dataset.prefix,
        offset=p.dataset.offset,
        suffix=p.dataset.suffix,
        reference_pdb=p.input.reference_pdb,
        reference_mtz=p.input.reference_mtz,
        ground_state_datasets=ground_state,
        cpus=p.analyse.cpus,
        min_build_datasets=p.analyse.min_build_datasets,
        max_build_datasets=p.analyse.max_build_datasets,
        max_new_datasets=p.analyse.max_new_datasets,
        no_lr_comp_check=p.analyse.no_lr_comp_check,
        log_time_out=p.run.log_time_out,
        dry_run=p.run.dry_run,
    )
    preparer.run()
    return {"status": "completed", "message": "pandda.analyse finished"}


def _run_pandda2(step, ctx):
    """Run pandda2.analyse.

    pandda2.analyse has a different CLI from pandda.analyse.
    This handler is a placeholder; parameter mapping needs verification
    against the installed pandda2 version before use.
    """
    raise NotImplementedError(
        "pandda2.analyse support is not yet implemented. "
        "Run pandda2.analyse manually or set analyser=pandda."
    )


_HANDLERS = {
    "pandda":  _run_pandda,
    "pandda2": _run_pandda2,
}


def run_step(step, ctx):
    analyser = step.pandda.analyser
    handler  = _HANDLERS.get(analyser)
    if handler is None:
        return {"status": "error", "fatal": True,
                "message": f"unknown analyser '{analyser}'"}
    return handler(step, ctx)
