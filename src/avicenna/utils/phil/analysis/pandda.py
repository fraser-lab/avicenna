"""PanDDA 1 (pandda.analyse) backend for the analysis sub-pipeline.

Bundled with CCP4 7.0.  Selected by ``program = pandda`` at the step level
(pandda2 is a sibling backend).  Program-agnostic input (the refine output,
reference structure, ground-state datasets, ligands) lives in the shared
``analysis.input`` scope.
"""

SCOPE_NAME = "pandda"

PROGRAMS = {
    "pandda": """\
pandda
  .short_caption = PanDDA Parameters
  .help = Parameters for the PanDDA (pandda.analyse) event-map analysis step.
{
  prefix = "VIC"
    .type = str
    .short_caption = Dataset prefix
    .help = Prefix applied to panDDA dataset names (e.g. VIC -> VIC_x0001). Code may substitute the protein of interest.
    .expert_level = 0
  copy_input = True
    .type = bool
    .short_caption = Copy input
    .help = True copies the input PDB/MTZ into pandda/data/<prefix>_x#### (conservative. duplicates the final PDB/MTZ per dataset). False creates symlinks to the originals instead (saves space, but the links break if the refine output is moved or deleted).
    .expert_level = 1
  remove_waters = False
    .type = bool
    .short_caption = Remove waters
    .help = Strip ordered solvent (HOH records) from each input model before panDDA. A water-free model changes how pandda.analyse builds event maps. Doing it here -- rather than refining without waters -- keeps ordered solvent in refinement (where it helps, especially at high resolution) while still giving panDDA the water-free model. Only HOH is removed. ligands, ions, and structural cofactors are kept. When set, the model is written as a filtered copy even in symlink mode (copy_input=False).
    .expert_level = 0
  dataset
    .short_caption = Dataset options
    .help = Controls dataset naming and selection.
  {
    offset = auto
      .type = int
      .short_caption = Numbering offset
      .help = Demarcation between the ground-state and the analysed (ligand) \
              datasets. Ligand datasets are numbered starting at offset+1, so the \
              leading offset slots are reserved for ground-state datasets. auto \
              sets it to the number of ground-state datasets found in \
              input.ground_state_datasets (contiguous packing). A user value \
              supersedes the calculation and must be at least the number of \
              ground-state datasets found.
      .expert_level = 1
    ground_state_range = auto
      .type = str
      .short_caption = Ground-state range
      .help = Dataset tag range flagged to pandda.analyse as ground state, e.g. \
              "x0001-x0030" (or "1-30"). auto flags every dataset found in \
              input.ground_state_datasets. A user value supersedes the \
              calculation and must fall within 1..offset (a value beyond offset \
              is a terminal error). It is ignored when no ground-state folder is \
              given.
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

def _load_records(source):
    """The dataset records of <source>/info.json, by dataset tag."""
    from avicenna.utils.info import ProjectInfo
    return dict(ProjectInfo.load(str(source)).datasets)


def _selected_libraries(inp):
    """The ligand_library selection as a filter: None means every library."""
    from avicenna.utils.phil.analysis import LIGAND_LIBRARIES, resolve_ligand_libraries
    selected = resolve_ligand_libraries(getattr(inp, "ligand_library", None))
    return None if set(selected) == set(LIGAND_LIBRARIES) else selected


def make_preparer(inp, p, dry_run=None, on_submit=None, project_dir=None):
    """Build a PanDDAPreparer from the shared ``input`` scope and a ``pandda``
    step block.  Shared by the interpreter (run_step) and the CLI --legacy path.

    ``dry_run`` overrides ``p.run.dry_run`` when not None.  Raises ValueError if
    ``input.input_datasets`` is unset or ``input.ground_state_datasets`` is set
    but not a directory.  Offset / ground-state-range resolution (and their
    validation) happens in PanDDAPreparer.run once the dataset counts are known.
    """
    import pathlib
    from avicenna.utils.phil.analysis.pandda_preparer import PanDDAPreparer

    source = getattr(inp, "input_datasets", None) if inp is not None else None
    if not source:
        raise ValueError("input.input_datasets is not set")
    ref_pdb = getattr(inp, "reference_pdb", None) if inp is not None else None
    ref_mtz = getattr(inp, "reference_mtz", None) if inp is not None else None

    # Ground-state (apo) datasets live in their own folder, same layout as the
    # input datasets (a project dir with qrefine/<job>/final_refined.{pdb,mtz}).
    # When given, they become the leading PanDDA datasets; offset and the
    # ground-state tag range are resolved against them in PanDDAPreparer.run.
    gs_folder = getattr(inp, "ground_state_datasets", None) if inp is not None else None
    gs_dirs = []
    if gs_folder:
        gs_path = pathlib.Path(str(gs_folder))
        if not gs_path.is_dir():
            raise ValueError(
                f"input.ground_state_datasets is not a directory: {gs_folder}")
        gs_dirs = [gs_path]

    records = _load_records(source)
    return PanDDAPreparer(
        process_dirs=[source],
        ground_state_dirs=gs_dirs,
        prefix=p.prefix,
        offset=p.dataset.offset,
        reference_pdb=ref_pdb,
        reference_mtz=ref_mtz,
        ground_state_range=p.dataset.ground_state_range,
        copy_input=p.copy_input,
        remove_waters=p.remove_waters,
        cpus=p.analyse.cpus,
        min_build_datasets=p.analyse.min_build_datasets,
        max_build_datasets=p.analyse.max_build_datasets,
        max_new_datasets=p.analyse.max_new_datasets,
        no_lr_comp_check=p.analyse.no_lr_comp_check,
        log_time_out=p.run.log_time_out,
        dry_run=p.run.dry_run if dry_run is None else dry_run,
        on_submit=on_submit,
        project_dir=project_dir,
        records=records,
        ligand_source_path=getattr(inp, "ligand_source_path", None),
        ligand_libraries=_selected_libraries(inp),
    )


def run_step(step, ctx):
    """Run pandda.analyse via PanDDAPreparer.

    Program-agnostic input comes from the shared ``analysis.input`` scope,
    passed in via ``ctx['input']`` by the interpreter.
    """
    from avicenna.utils.utils import SENTINEL_WAIT_DONE, SENTINEL_WAIT_FAILED

    try:
        preparer = make_preparer(ctx.get("input"), step.pandda,
                                 on_submit=ctx.get("on_submit"),
                                 project_dir=ctx.get("project_dir"))
        outcome = preparer.run()
    except ValueError as e:
        return {"status": "error", "fatal": True, "message": str(e)}
    if outcome == SENTINEL_WAIT_FAILED:
        return {"status": "error", "fatal": True,
                "message": "pandda.analyse did not finish"}
    if outcome == SENTINEL_WAIT_DONE:
        return {"status": "completed", "message": "pandda.analyse finished"}
    return {"status": "completed", "message": "pandda.analyse submitted"}
