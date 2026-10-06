"""
PHIL-based parameter system for the Avicenna pipeline.

Classes
-------
PhilBase        -- abstract base: PHIL definition, adopt/export helpers
ProcessingPHIL  -- parameters for the data-processing sub-pipeline (vic.process)
RefinePHIL      -- parameters for the refinement sub-pipeline (vic.refine)
AnalysisPHIL    -- parameters for the analysis sub-pipeline (vic.pandda)
AvicennaPHIL    -- master scope; discovers and nests all sub-pipeline scopes,
                   then applies shipped default pipeline sequences

Sub-pipeline plugin interface
------------------------------
Create a Python package (directory with __init__.py) inside this directory.
The package must export a module-level string ``PHIL_DEF`` whose content is a
single top-level PHIL scope (e.g. ``mosflm_processing { ... }``).  The scope
name must be unique across all plugins.  ``AvicennaPHIL`` discovers and nests
it automatically; default step sequences belong in a companion
``resources/pipelines/default_<name>.phil`` file.
"""

import importlib
import pkgutil
from pathlib import Path

import freephil

from avicenna.utils.phil._base import PhilBase
from avicenna.utils.phil.processing import ProcessingPHIL
from avicenna.utils.phil.refine import RefinePHIL
from avicenna.utils.phil.analysis import AnalysisPHIL, PanDDAPHIL

__all__ = [
    "PhilBase",
    "ProcessingPHIL",
    "RefinePHIL",
    "AnalysisPHIL",
    "PanDDAPHIL",
    "AvicennaPHIL",
]

# ---------------------------------------------------------------------------
# Master PHIL preamble (project scope -- not a sub-pipeline plugin)
# ---------------------------------------------------------------------------

_AVICENNA_PREAMBLE = """\
avicenna
  .short_caption = Avicenna
  .help = Master scope for the full Avicenna pipeline.
{
  project
    .short_caption = Project
    .help = Top-level project metadata shared across sub-pipelines.
  {
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
  }
"""

# ---------------------------------------------------------------------------
# Sub-pipeline discovery
# ---------------------------------------------------------------------------

def _discover_subpipeline_defs() -> list[str]:
    """Return PHIL_DEF strings from every sub-pipeline package in this directory."""
    package_dir = Path(__file__).parent
    defs = []
    for _finder, name, ispkg in pkgutil.iter_modules([str(package_dir)]):
        if not ispkg or name.startswith("_"):
            continue
        mod = importlib.import_module(f"avicenna.utils.phil.{name}")
        if hasattr(mod, "PHIL_DEF"):
            defs.append(mod.PHIL_DEF)
    return defs


def _build_avicenna_phil() -> str:
    return _AVICENNA_PREAMBLE + "".join(_discover_subpipeline_defs()) + "}\n"

# ---------------------------------------------------------------------------
# Default pipeline loader
# ---------------------------------------------------------------------------

_PIPELINES_DIR = Path(__file__).parent.parent.parent / "resources" / "pipelines"


def _load_default_phils() -> list:
    """Parse all *.phil files from resources/pipelines/ and return a list of
    freephil scope objects suitable for passing to master.fetch(sources=...).

    Resource files use sub-pipeline scope (e.g. ``processing { steps { } }``).
    They are wrapped in ``avicenna { }`` here so they match the master scope."""
    sources = []
    if _PIPELINES_DIR.is_dir():
        for phil_file in sorted(_PIPELINES_DIR.glob("*.phil")):
            content = phil_file.read_text()
            sources.append(freephil.parse(f"avicenna {{\n{content}\n}}\n"))
    return sources

# ---------------------------------------------------------------------------
# Master PHIL class
# ---------------------------------------------------------------------------

class AvicennaPHIL(PhilBase):
    """Master PHIL scope nesting all sub-pipeline scopes.

    Sub-pipeline scopes are discovered automatically from sub-packages in this
    directory.  Default step sequences are loaded from
    ``resources/pipelines/*.phil`` at construction time so that
    ``params.avicenna.<pipeline>.steps`` is pre-populated with the shipped
    defaults.

    Downstream code reads parameters via
    ``phil.params.avicenna.<pipeline>.steps`` (step sequence) and
    ``phil.params.avicenna.<pipeline>.steps[i].<program>.*`` (step params).
    """

    def __init__(self, load_defaults: bool = True):
        self._master = freephil.parse(_build_avicenna_phil())
        self._default_sources = _load_default_phils() if load_defaults else []
        self._params = self._master.fetch(sources=self._default_sources).extract()

    def adopt_phil_string(self, phil_string: str):
        """Merge a PHIL string into the current params, preserving loaded defaults."""
        user_phil = freephil.parse(phil_string)
        self._params = self._master.fetch(
            sources=[*self._default_sources, user_phil]
        ).extract()

    def from_file(self, path: str):
        """Read a .phil file and merge it into the current params, preserving defaults."""
        with open(path) as fh:
            self.adopt_phil_string(fh.read())
