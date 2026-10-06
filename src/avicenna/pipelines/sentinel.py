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

import os
import stat
import time
from pathlib import Path

from avicenna.pipelines.jobbase import RuntimeLimited
from avicenna.pipelines.sge import SGESubmitter
from avicenna.utils.utils import (SENTINEL_CONTEXT, SENTINEL_DONE,
                                 SENTINEL_FAILED, echo_log, log_offset,
                                 qsub_terse, sentinel_outcome,
                                 wait_for_sentinel)


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


class PipelineSentinel(RuntimeLimited, SGESubmitter):
    """Base completion sentinel.  Subclass per sub-pipeline.

    Class attributes a subclass sets:
        WATCHER  -- a ``WatcherBase`` subclass (supplies ``is_running`` + omnibus log)
        _SUBDIR  -- subdir of project_dir the watcher scans ("" = project_dir itself)
        _TAG     -- short label used for the generated worker/submit filenames
    """

    WATCHER = None
    _SUBDIR = ""
    _TAG = "run"
    MARKER = SENTINEL_DONE
    FAILURE_MARKER = SENTINEL_FAILED
    POLL_INTERVAL = 30          # seconds between sweeps while work is still running
    CONFIRM_INTERVAL = 8        # seconds between the (idle) confirmation sweeps
    IDLE_CONFIRMATIONS = 2      # consecutive idle sweeps required to declare done
    H_RT = "01:00:00"

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
        appearing cannot be mistaken for completion.

        The completion marker is written only if finalization succeeded: it is
        the one signal the foreground sees, so it has to mean that the derived
        files a later stage reads were actually produced."""
        idle = 0
        while idle < self.IDLE_CONFIRMATIONS:
            idle = 0 if self._watcher().is_running() else idle + 1
            if idle < self.IDLE_CONFIRMATIONS:
                # Poll slowly while work remains (keeps NFS sweep load low over a
                # long run); once the first idle sweep lands, confirm quickly so
                # finalization isn't delayed by a full poll interval.  Stragglers
                # (forced-integration arrays) run for minutes, so the short
                # confirmation gap still catches them reliably.
                time.sleep(self.CONFIRM_INTERVAL if idle else self.POLL_INTERVAL)
        try:
            self.on_complete()
        except Exception as exc:
            # Loud in all three places somebody looks: the omnibus log, this
            # job's own output, and the exit status.  The traceback follows the
            # summary line, so re-raising is what puts it in the job's log.
            self._log("ERROR", f"{self.FAILURE_MARKER} {type(exc).__name__}: {exc}")
            raise
        else:
            self._write_marker()
        finally:
            # The cluster jobs really are gone either way, so holding the lock
            # after a failed finalization would only block a re-run.
            self._release_lock()

    def on_complete(self):
        """Finalization hook -- override in subclasses."""

    def _on_runtime_limit(self):
        """The run ends failed, as for a failed finalization, and the lock is
        released so a re-run is not blocked."""
        self._log("ERROR", f"{self.FAILURE_MARKER} run-time limit reached "
                           f"-- sentinel stopped")
        self._release_lock()

    def _log(self, tag, message):
        """One line to the omnibus log and to this job's own output.

        A line that cannot be written must not end a finished run, so the
        omnibus write is allowed to fail; stdout still carries it into the
        sentinel's SGE output file.
        """
        from avicenna.utils.utils import format_log_line
        line = format_log_line(tag, message, context=SENTINEL_CONTEXT)
        try:
            with open(self.omnibus, "a") as fh:
                fh.write(line)
        except OSError:
            pass
        print(line, end="", flush=True)

    def wait(self, start=0, **kwargs):
        """Foreground side: wait for this sentinel's outcome, then print what
        it wrote to the omnibus log, so the terminal shows the log in full.

        *start* is where this run's lines begin (see ``wait_for_sentinel``).
        The sentinel writes from the cluster, so its lines reach the terminal
        only by being read back.  It finalizes only after the jobs have been
        idle for repeated sweeps, by which time the foreground has stopped
        writing to the log.  Printed after the wait rather than during it,
        because the wait's spinner redraws the terminal.
        """
        echo_from = log_offset(self.omnibus)
        try:
            return wait_for_sentinel(self.omnibus, start=start, **kwargs)
        finally:
            echo_log(self.omnibus, echo_from)

    def _write_block(self, lines):
        """A table set off from the log lines by rules, to the omnibus log
        and this job's own output; nothing when there are no lines.  Like a
        log line, it must not end a finished run if it cannot be written."""
        if not lines:
            return
        rule = "=" * max(len(line) for line in lines)
        text = "\n".join([rule, *lines, rule]) + "\n"
        try:
            with open(self.omnibus, "a") as fh:
                fh.write(text)
        except OSError:
            pass
        print(text, end="", flush=True)

    def _write_marker(self):
        """Record completion.  Deliberately not tolerant of a write failure: a
        marker nobody can read is not a completion signal, so the run ends
        unmarked and non-zero rather than silently unreported."""
        from avicenna.utils.utils import format_log_line
        with open(self.omnibus, "a") as fh:
            fh.write(format_log_line("DONE", self.MARKER, context=SENTINEL_CONTEXT))

    def _release_lock(self):
        """Release the project directory's run lock, if this job holds it.

        The sentinel is the last thing to run in every mode, which is what makes
        it the right place: the foreground CLI may have exited hours earlier.
        It matches on its own SGE job id so a lock belonging to a later run is
        left alone, and a failure here must never be what ends a finished run.
        """
        from avicenna.utils.runlock import RunLock
        try:
            RunLock.release_for(self.project_dir, os.environ.get("JOB_ID"))
        except Exception as exc:
            print(f"WARNING: could not release the run lock: {exc}")

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

        submit_sh = self._write_sge_script(
            self.project_dir / f"sentinel_{self._TAG}_submit.sh",
            f"./{worker.name}",
            h_rt=self.H_RT, slots=1, mem_free="1G", hold_jid=hold_job_id,
            logs_dir=logs_dir or self.project_dir,
            preamble=f"cd {self.project_dir}\n",
        )
        return qsub_terse([str(submit_sh)])


# ---------------------------------------------------------------------------
# Processing -> gathers info.json, exports qrefine_list.txt, writes refine.phil
# ---------------------------------------------------------------------------

class ProcessingSentinel(PipelineSentinel):
    _SUBDIR = "processing"
    _TAG    = "processing"

    def __init__(self, project_dir, no_filter=False):
        super().__init__(project_dir)
        self.no_filter = no_filter
        self.gather = None

    @property
    def WATCHER(self):
        from avicenna.utils.utils import ProcessingWatcher
        return ProcessingWatcher

    def _worker_kwargs(self):
        return {"no_filter": self.no_filter}

    def on_complete(self):
        from avicenna.utils.utils import ProcessingScraper, export_qrefine_list
        # The project record first: it is the run's account of itself, so a
        # failure in the derived files still leaves one.
        self._merge_info_json()
        out = self.project_dir / "qrefine_list.txt"
        if self.no_filter:
            # Testing only: every completed result, unselected.
            scraper = ProcessingScraper(str(self._procpath()))
            scraper.scrape_all()
            scraper.write_qrefine_list(path=str(out))
        else:
            export_qrefine_list(self.project_dir, out)
        self._write_refine_phil()

    def _merge_info_json(self):
        """Gather this stage's per-process records into info.json.

        Absent means the run was started without one, which is not an error:
        info.json is additive metadata, and a project that has none processes
        exactly as it did before.  An unreadable record is reported per dataset
        and counted, since file-system damage rarely arrives alone.
        """
        from avicenna.pipelines.gather import ProcessingGather, duplicate_report
        from avicenna.utils.info import FILENAME, ProjectInfo

        if not os.path.isfile(self.project_dir / FILENAME):
            return
        info = ProjectInfo.load(self.project_dir)
        self.gather = ProcessingGather(self._procpath())
        report = self.gather.run(info)
        for dataset_key, preserved in report.damaged:
            self._log("ERROR", f"{dataset_key}: its record could not be read "
                               f"and was preserved as {preserved}")
        info.write()
        self._log("INFO", f"{os.path.basename(info.path)}: {report.summary()}")
        self._write_block(duplicate_report(info, report.decisions))

    def _write_refine_phil(self):
        """Emit refine.phil (defaults + run paths) for the next stage; the user
        edits input.reference_pdb then runs vic.refine."""
        from avicenna.utils.phil import RefinePHIL
        phil = RefinePHIL()
        phil.adopt_phil_string(
            f'refine {{\n  input {{\n'
            f'    output_dir = "{self.project_dir}"\n'
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

    def __init__(self, project_dir, dataset_list=None):
        super().__init__(project_dir)
        self.dataset_list = str(dataset_list) if dataset_list else None

    def _worker_kwargs(self):
        return {"dataset_list": self.dataset_list} if self.dataset_list else {}

    def _watcher(self):
        return self.WATCHER(procpath=str(self._procpath()),
                            dataset_list=self.dataset_list)

    def on_complete(self):
        from avicenna.utils.utils import QRefineScraper
        # The project record first, as for processing.
        self._merge_info_json()
        QRefineScraper(str(self._procpath())).scrape_all()
        self._write_analysis_phil()

    def _merge_info_json(self):
        """Gather the refinement job records into info.json."""
        from avicenna.pipelines.gather import RefinementGather
        from avicenna.pipelines.refinement_runner import load_datasets
        from avicenna.utils.info import FILENAME, ProjectInfo

        if not os.path.isfile(self.project_dir / FILENAME):
            self._log("WARN", f"no {FILENAME} in {self.project_dir}; "
                              f"refinement results not recorded")
            return
        info = ProjectInfo.load(self.project_dir)
        tags = [d.pin for d in load_datasets(self.project_dir, self.dataset_list)]
        gather = RefinementGather(self.project_dir / "qrefine", tags)
        report = gather.run(info)
        for dataset_key, preserved in report.damaged:
            self._log("ERROR", f"{dataset_key}: its refinement record could not "
                               f"be read and was preserved as {preserved}")
        if gather.unknown:
            self._log("WARN", f"not in {FILENAME}, so not recorded: "
                              f"{', '.join(gather.unknown)}")
        info.write()
        self._log("INFO", f"{os.path.basename(info.path)}: {report.summary()}")

    def _write_analysis_phil(self):
        from avicenna.utils.phil import AnalysisPHIL
        phil = AnalysisPHIL()
        phil.adopt_phil_string(
            f'analysis {{\n  input {{\n'
            f'    input_datasets = "{self.project_dir}"\n'
            f'  }}\n}}\n'
        )
        (self.project_dir / "analysis.phil").write_text(phil.full_phil_string())


# ---------------------------------------------------------------------------
# Analysis -> gathers the panDDA results into info.json
# ---------------------------------------------------------------------------

class AnalysisSentinel(PipelineSentinel):
    """Held on the single pandda.analyse job.

    The hold alone means the work is over: nothing is submitted after it, so
    there is nothing to sweep for.  *source* is the project whose refinement
    results were analysed, and whose info.json is updated.
    """

    _TAG = "analysis"
    IDLE_CONFIRMATIONS = 0
    FOLLOW_POLL = 5             # seconds between reads of the pandda log

    def __init__(self, project_dir, source):
        super().__init__(project_dir)
        self.source = Path(source).resolve()

    @property
    def omnibus(self):
        return self.project_dir / "analysis_pipeline.log"

    def _worker_kwargs(self):
        return {"source": str(self.source)}

    def on_complete(self):
        from avicenna.pipelines.gather import AnalysisGather, write_info_csv
        from avicenna.utils.info import FILENAME, ProjectInfo

        # The record first, so a run that did not finish still says so per
        # dataset; then the run itself is failed.
        if os.path.isfile(self.source / FILENAME):
            info = ProjectInfo.load(self.source)
            report = AnalysisGather(self.project_dir).run(info)
            info.write()
            self._log("INFO", f"{FILENAME}: {report.summary()}")
            rows = write_info_csv(info, self.source / "info.csv")
            self._log("INFO", f"info.csv: {rows} analysed dataset(s)")
        else:
            self._log("WARN", f"no {FILENAME} in {self.source}; "
                              f"analysis results not recorded")
        if not (self.project_dir / "pandda" / "pandda.done").is_file():
            raise RuntimeError("pandda.analyse did not finish (no pandda.done)")

    # ------------------------------------------------------------------
    # Foreground
    # ------------------------------------------------------------------

    def follow(self, start=0, log_time_out=0):
        """Print the pandda log as it grows until this sentinel reports an
        outcome, then the sentinel's own lines; return the outcome.

        panDDA writes its own log, so the terminal shows it by reading it back.
        *log_time_out* (minutes, 0 = none) bounds the wait for that log to
        appear; on expiry the outcome is None and the sentinel still finishes
        the run.  Interrupting only stops the following.
        """
        logs_dir = self.project_dir / "pandda" / "logs"
        echo_from = log_offset(self.omnibus)
        deadline = time.time() + log_time_out * 60 if log_time_out else None
        log_path, offset = None, 0
        print(f"Waiting for pandda log in {logs_dir} ...", flush=True)
        try:
            while True:
                outcome = sentinel_outcome(self.omnibus, start)
                if log_path is None:
                    logs = sorted(logs_dir.glob("pandda-*.log"),
                                  key=lambda p: p.stat().st_mtime)
                    if logs:
                        log_path = logs[-1]
                        print(f"Following {log_path.name}", flush=True)
                if log_path is not None:
                    offset = echo_log(log_path, offset)
                if outcome:
                    return outcome
                if log_path is None and deadline and time.time() > deadline:
                    print("Timed out waiting for the pandda log; the completion "
                          "sentinel will still finish the run.")
                    return None
                time.sleep(self.FOLLOW_POLL)
        finally:
            echo_log(self.omnibus, echo_from)
