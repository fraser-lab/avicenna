"""Shared base for AVICENNA sub-pipeline jobs that run external programs on a
compute node (the per-dataset processing interpreter, the per-suffix refinement
interpreter, ...).

Stall watchdog
--------------
A blunt wall-clock cap would kill legitimately-long work -- a large XDS
integration or the multi-minute copy of an image set to local scratch both run
for a long time while making steady progress.  The discriminator between
"running long" and "hung" is PROGRESS, not elapsed time: a healthy job keeps
writing files (XDS streams its ``.LP`` logs per image batch; refinement programs
stream their logs to disk -- see ``utils/phil/refine/qrefine._run_cmd``).

So the watchdog watches the newest file mtime under the job's working roots and
fires only after ``STALL_WINDOW_S`` seconds with no write anywhere.  On a stall
it logs a terminal ``ERROR`` line (so the watcher/sentinel see the dataset as
FAILED) and hard-exits, which ends the SGE task -> releases the sentinel's hold
-> lets finalization proceed.

Known limitation: a fully wedged process (uninterruptible ``D`` state on NFS)
cannot run its own monitor thread; only an external ``qdel``-based monitor would
catch that.  See the ``stall-detection-and-completion-disconnect`` project memory
for the recorded child-process / external-monitor alternatives.  The SGE
``h_rt`` remains the ultimate backstop.
"""

import contextlib
import os
import sys
import threading
import time
from pathlib import Path


def _has_recent_activity(roots, since):
    """Return True as soon as any file or directory under *roots* has an mtime
    >= *since*.  Short-circuits on the first hit, so a healthy (progressing) job
    is cheap to check; only a genuinely stalled job walks the full tree.
    Missing roots are skipped (os.walk yields nothing)."""
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            try:
                if os.stat(dirpath).st_mtime >= since:
                    return True
            except OSError:
                pass
            for name in files:
                try:
                    if os.stat(os.path.join(dirpath, name)).st_mtime >= since:
                        return True
                except OSError:
                    continue
    return False


class SubPipelineJob:
    """Mixin providing the progress-based stall watchdog.

    Subclasses (the interpreters) override ``_watch_roots`` and the stall-log
    hooks, and wrap their run body in ``with self._stall_watchdog():``.
    """

    STALL_WINDOW_S = 900     # seconds with no file activity before "hung"
    STALL_POLL_S   = 60      # how often the monitor checks for progress

    # -- hooks for subclasses ------------------------------------------------

    def _watch_roots(self):
        """Directories whose newest file mtime signals progress.  Paths that do
        not exist yet (e.g. a scratch dir created mid-run) are allowed -- they
        start contributing once created."""
        return []

    def _stall_log_path(self):
        """Path to the pipeline.log that should receive the terminal ERROR
        line on a stall (so the watcher/sentinel see FAILED), or None."""
        return None

    def _stall_context(self):
        """Context label for the stall log line (e.g. the dataset id)."""
        return None

    def _stall_window(self):
        override = getattr(self, "params", {}).get("stall_window_s")
        return int(override) if override else self.STALL_WINDOW_S

    # -- watchdog ------------------------------------------------------------

    @contextlib.contextmanager
    def _stall_watchdog(self):
        window = self._stall_window()
        roots  = [str(r) for r in self._watch_roots() if r]
        if not roots or window <= 0:
            yield
            return
        stop = threading.Event()

        def _monitor():
            last = time.time()        # grace: first window runs from job start
            while not stop.wait(self.STALL_POLL_S):
                if _has_recent_activity(roots, last):
                    last = time.time()
                elif time.time() - last > window:
                    self._on_stall(int(time.time() - last))
                    os._exit(1)

        threading.Thread(target=_monitor, daemon=True).start()
        try:
            yield
        finally:
            stop.set()

    def _on_stall(self, idle_s):
        from avicenna.utils.utils import format_log_line
        line = format_log_line(
            "ERROR",
            f"no progress for {idle_s}s -- job marked hung and terminated",
            context=self._stall_context(),
        )
        path = self._stall_log_path()
        if path:
            try:
                with open(path, "a") as fh:
                    fh.write(line)
            except OSError:
                pass
        sys.stderr.write(line)
        sys.stderr.flush()
