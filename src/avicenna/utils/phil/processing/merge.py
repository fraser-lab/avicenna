"""CCP4/Phenix merging-and-statistics backend for the processing sub-pipeline.

Groups the four post-integration programs (Pointless, Aimless, freerflag/
CCP4 truncate, and Xtriage) that together constitute a single "merge and
evaluate" stage.  Each program appears as an individual step in the
pipeline sequence; this module contributes all four to the step schema.
"""

SCOPE_NAME = "merge"

PROGRAMS = {
    "pointless": """\
pointless
  .short_caption = Pointless Parameters
  .help = Parameters for the Pointless space-group determination step.
{
}
""",
    "aimless": """\
aimless
  .short_caption = Aimless Parameters
  .help = Parameters for the Aimless scaling and merging step.
{
  apply_res_cutoff = False
    .type = bool
    .short_caption = Apply resolution cutoff
    .help = If True, apply the CC1/2-based resolution cutoff during scaling.
    .expert_level = 0
}
""",
    "rfree": """\
rfree
  .short_caption = R-free Parameters
  .help = Parameters for the R-free flag generation step (freerflag / ctruncate).
{
}
""",
    "xtriage": """\
xtriage
  .short_caption = Xtriage Parameters
  .help = Parameters for the Phenix Xtriage data-quality analysis step.
{
}
""",
}
