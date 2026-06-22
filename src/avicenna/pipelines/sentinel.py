"""SGE completion sentinels: a held job per sub-pipeline that runs the
finalization (scrape, filter, write the *next* stage's PHIL, completion marker)
once all the sub-pipeline's cluster jobs are done -- independent of any
foreground process.

Why
---
Finalization must happen on job COMPLETION, in every run mode -- interactive,
``--silent``, or after the submitting shell disconnects.  Previously it hung off
the foreground ``Watcher`` loop, so a dropped connection skipped it (no
``refine.phil``, no filtered ``qrefine_list.txt``, no summary).  The sentinel is
submitted with an SGE hold on the main array, so the scheduler releases it only
after the bulk work finishes.  It then polls the *scheduler-agnostic*
file-structure ``Watcher.is_running()`` to catch any dynamically-submitted
stragglers (e.g. forced-integration arrays) before running the finalization
hook.  The sentinel -- not the foreground -- is the sole arbiter of completion.

Generic by design
-----------------
``PipelineSentinel`` is the base; each sub-pipeline is a thin subclass that sets
``WATCHER`` / ``_SUBDIR`` / ``_TAG`` and implements ``on_complete``.  Only the SGE
submission helpers (``qsub_terse``, the ``-hold_jid`` submit script) are
scheduler-specific; done-detection is the file sweep, so swapping schedulers
touches only the submit path.
"""

import stat
import time
from pathlib import Path

from avicenna.utils.utils import qsub_terse


_WORKER_TEMPLATE = '''#!/usr/bin/env python3
import os, subprocess, sys
sys.path.insert(0, {src_dir})
shell = os.environ.get("SHELL", "/bin/bash")
if any(s in shell for s in ("csh", "tcsh")):
    _cmd = "tcsh -c 'source /programs/sbgrid.cshrc && env'"
else:
    _cmd = "bash --norc --noprofile -c 'source /programs/sbgrid.shrc && env'"
_r = subprocess.run(_cmd, shell=True, capture_output=True, text=True)
for _l in _r.stdout.splitlines():
    if "=" in _l:
        _k, _, _v = _l.partition("=")
        if _k.isidentifier():
            os.environ[_k] = _v
from {mod} import {cls}
{cls}({project_dir}{kwargs}).run()
'''


class PipelineSentinel:
    """Base completion sentinel.  Subclass per sub-pipeline.

    Class attributes a subclass sets:
        WATCHER  -- a ``WatcherBase`` subclass (supplies ``is_running`` + omnibus log)
        _SUBDIR  -- subdir of project_dir the watcher scans ("" = project_dir itself)
        _TAG     -- short label used for the generated worker/submit filenames
    """

    WATCHER = None
    _SUBDIR = ""
    _TAG = "run"
    MARKER = "Run complete."
    POLL_INTERVAL = 30          # seconds between is_running() sweeps
    IDLE_CONFIRMATIONS = 2      # consecutive idle sweeps required to declare done

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir).resolve()

    # ------------------------------------------------------------------
    # Watcher wiring
    # ------------------------------------------------------------------

    def _procpath(self):
        return self.project_dir / self._SUBDIR if self._SUBDIR else self.project_dir

    def _watcher(self):
        return self.WATCHER(procpath=str(self._procpath()))

    @property
    def omnibus(self):
        return self.project_dir / self.WATCHER._OMNIBUS_LOG

    # ------------------------------------------------------------------
    # Compute-node entry point
    # ------------------------------------------------------------------

    def run(self):
        """Poll until the sub-pipeline is idle, then finalize and mark complete.

        Requires IDLE_CONFIRMATIONS consecutive idle sweeps so the brief window
        between the initial array finishing and a forced-integration array
        appearing cannot be mistaken for completion."""
        idle = 0
        while idle < self.IDLE_CONFIRMATIONS:
            idle = 0 if self._watcher().is_running() else idle + 1
            if idle < self.IDLE_CONFIRMATIONS:
                time.sleep(self.POLL_INTERVAL)
        try:
            self.on_complete()
        finally:
            self._write_marker()

    def on_complete(self):
        """Finalization hook -- override in subclasses."""

    def _write_marker(self):
        from avicenna.utils.utils import format_log_line
        with open(self.omnibus, "a") as fh:
            fh.write(format_log_line("DONE", self.MARKER, context="sentinel"))

    # ------------------------------------------------------------------
    # Submit-host: write worker + submit a held SGE job
    # ------------------------------------------------------------------

    def _worker_kwargs(self):
        """Extra keyword args (besides project_dir) baked into the worker call."""
        return {}

    def submit(self, hold_job_id, logs_dir=None):
        """Write the sentinel worker + submit script and qsub it held on
        *hold_job_id*.  Returns the sentinel's own job id (or None)."""
        import avicenna
        src_dir = str(Path(avicenna.__file__).resolve().parents[1])
        kw = "".join(f", {k}={v!r}" for k, v in self._worker_kwargs().items())
        worker = self.project_dir / f"sentinel_{self._TAG}.py"
        worker.write_text(_WORKER_TEMPLATE.format(
            src_dir=repr(src_dir),
            mod=type(self).__module__,
            cls=type(self).__name__,
            project_dir=repr(str(self.project_dir)),
            kwargs=kw,
        ))
        worker.chmod(worker.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)

        logs_dir = logs_dir or self.project_dir
        submit_sh = self.project_dir / f"sentinel_{self._TAG}_submit.sh"
        submit_sh.write_text(
            "#!/bin/bash\n"
            "#$ -S /bin/bash\n"
            "#$ -cwd\n"
            "#$ -j y\n"
            "#$ -pe smp 1\n"
            "#$ -l mem_free=1G\n"
            "#$ -l h_rt=01:00:00\n"
            f"#$ -hold_jid {hold_job_id}\n"
            f"#$ -o {logs_dir}\n\n"
            f"cd {self.project_dir}\n"
            f"./{worker.name}\n"
        )
        submit_sh.chmod(submit_sh.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
        return qsub_terse([str(submit_sh)])


# ---------------------------------------------------------------------------
# Processing -> writes filtered qrefine_list.txt + refine.phil
# ---------------------------------------------------------------------------

class ProcessingSentinel(PipelineSentinel):
    _SUBDIR = "processing"
    _TAG    = "processing"

    def __init__(self, project_dir, no_filter=False):
        super().__init__(project_dir)
        self.no_filter = no_filter

    @property
    def WATCHER(self):
        from avicenna.utils.utils import ProcessingWatcher
        return ProcessingWatcher

    def _worker_kwargs(self):
        return {"no_filter": self.no_filter}

    def on_complete(self):
        from avicenna.utils.utils import ProcessingScraper, ProcessingFilter
        scraper = ProcessingScraper(str(self._procpath()))
        scraper.scrape_all()
        if not self.no_filter:
            scraper.results = ProcessingFilter().run(scraper.results)
        scraper.write_qrefine_list(path=str(self.project_dir / "qrefine_list.txt"))
        self._write_refine_phil()

    def _write_refine_phil(self):
        """Emit refine.phil (defaults + run paths) for the next stage; the user
        edits input.reference_pdb then runs vic.refine."""
        from avicenna.utils.phil import RefinePHIL
        phil = RefinePHIL()
        phil.adopt_phil_string(
            f'refine {{\n  input {{\n'
            f'    output_dir = "{self.project_dir}"\n'
            f'    dataset_list = "qrefine_list.txt"\n'
            f'  }}\n}}\n'
        )
        (self.project_dir / "refine.phil").write_text(phil.full_phil_string())


# ---------------------------------------------------------------------------
# Refinement -> writes analysis.phil
# ---------------------------------------------------------------------------

class RefinementSentinel(PipelineSentinel):
    _SUBDIR = ""          # QRefineWatcher scans project_dir (it descends into qrefine/)
    _TAG    = "refine"

    @property
    def WATCHER(self):
        from avicenna.utils.utils import QRefineWatcher
        return QRefineWatcher

    def on_complete(self):
        from avicenna.utils.utils import QRefineScraper
        QRefineScraper(str(self._procpath())).scrape_all()
        self._write_analysis_phil()

    def _write_analysis_phil(self):
        from avicenna.utils.phil import AnalysisPHIL
        phil = AnalysisPHIL()
        phil.adopt_phil_string(
            f'analysis {{\n  input {{\n'
            f'    data_directory = "{self.project_dir}"\n'
            f'  }}\n}}\n'
        )
        (self.project_dir / "analysis.phil").write_text(phil.full_phil_string())
