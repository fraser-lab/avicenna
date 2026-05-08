"""qRefine crystallographic refinement backend for the refine sub-pipeline."""

SCOPE_NAME = "qrefine"

PROGRAMS = {
    "qrefine": """\
qrefine
  .short_caption = qRefine Parameters
  .help = Parameters for the qRefine crystallographic refinement step.
{
}
""",
}
