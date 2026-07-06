"""PanDDA 2 (pandda2.analyse) backend for the analysis sub-pipeline.

STUB.  pandda2.analyse installs independently of CCP4 and has a different CLI
from pandda.analyse; the parameter mapping needs verification against the
installed pandda2 version before use.  Selected by ``program = pandda2`` at the
step level.  Program-agnostic input (refine output, reference structure,
ground-state datasets, ligands) lives in the shared ``analysis.input`` scope.
"""

SCOPE_NAME = "pandda2"

PROGRAMS = {
    "pandda2": """\
pandda2
  .short_caption = PanDDA 2 Parameters
  .help = Parameters for the PanDDA 2 (pandda2.analyse) event-map analysis step. STUB - options to be added.
{
  prefix = "VIC"
    .type = str
    .short_caption = Dataset prefix
    .help = Prefix applied to panDDA dataset names (e.g. VIC -> VIC_x0001). Code may substitute the protein of interest.
    .expert_level = 0
  copy_input = True
    .type = bool
    .short_caption = Copy input
    .help = True copies the input PDB/MTZ into pandda/data/<prefix>_x#### (conservative. leaves the refine output untouched but duplicates the final PDB/MTZ per dataset). False links them in place to avoid duplication.
    .expert_level = 1
}
""",
}


def run_step(step, ctx):
    """Run pandda2.analyse.  STUB - not yet implemented."""
    raise NotImplementedError(
        "pandda2.analyse support is not yet implemented. "
        "Run pandda2.analyse manually or set program = pandda."
    )
