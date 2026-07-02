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


class SubPipelineInterpreter(SubPipelineJob):
    """Shared logging + step-execution loop for every sub-pipeline interpreter
    (processing, refinement, analysis).

    Logging is uniform across sub-pipelines: one ``_log(tag, message)`` writes a
    ``format_log_line`` to the sub-pipeline's ``pipeline.log`` (when
    ``_log_path()`` is set) and echoes it to stdout.  The interpreter owns the
    ``<step.id>:`` label -- backends return a DETAIL-ONLY message and never their
    own step/program label, so a user's custom step name flows through unchanged.

    A "just run it" sub-pipeline (e.g. analysis/PanDDA) sets
    ``STREAMS_PROGRAM_OUTPUT = True``: its backend lets the child program's stdout
    flow straight to the terminal (PanDDA shows its own progress), while these
    status lines are still emitted around it.  This flag is documentation of that
    backend behaviour; capture-vs-stream lives in the backend (refinement/
    processing capture to per-step logs for the stall watchdog; analysis does
    not).

    Subclasses provide ``_log_context()``/``_log_path()`` and ``_invoke(step,
    ctx)`` (calls the registered handler with the sub-pipeline's own signature),
    and may override ``_pre_step`` / ``_on_fatal`` for sub-pipeline-specific flow.
    """

    STREAMS_PROGRAM_OUTPUT = False

    # -- logging -------------------------------------------------------------

    def _log_context(self):
        """Bracketed context label (job/dataset id), or None."""
        return None

    def _log_context_width(self):
        return getattr(self, "params", {}).get("context_width")

    def _log_path(self):
        """Path to this sub-pipeline's pipeline.log, or None for stdout only."""
        return None

    def _log_forced_dir(self):
        """Bracketed retry-job tag inserted before the message (processing's
        forced-integration jobs), or None."""
        return None

    def _log(self, tag, message):
        from avicenna.utils.utils import format_log_line
        line = format_log_line(tag, message, context=self._log_context(),
                               context_width=self._log_context_width(),
                               forced_dir=self._log_forced_dir())
        path = self._log_path()
        if path:
            try:
                with open(path, "a") as fh:
                    fh.write(line)
            except OSError:
                pass
        print(line, end="", flush=True)

    # -- step loop -----------------------------------------------------------

    def _invoke(self, step, ctx):
        """Call the registered handler for *step* with the sub-pipeline's own
        argument signature and return its result dict."""
        raise NotImplementedError

    def _pre_step(self, step, ctx):
        """Hook run before the branching guards.  Return True to consume the step
        (the override is responsible for its own logging/bookkeeping); False to
        proceed normally."""
        return False

    def _on_fatal(self, step, ctx, result):
        """Hook invoked on a fatal error, before the loop breaks."""
        return None

    def _run_steps(self, steps, ctx):
        """Execute *steps* in order, logging one ``<tag> <step.id>: <message>``
        line each.  Returns True if a fatal error broke the loop.

        ctx must already carry the bookkeeping sets/maps (``last_by_program``,
        ``history_by_program``, ``completed_steps``, ``failed_steps``)."""
        for step in steps:
            # Every legitimate step has an id.  An id=None step is a stray CLI
            # override that could not be matched to a real step (see
            # PhilBase._merge_user_steps); never run it -- that would duplicate a
            # program (e.g. a second PanDDA pass).
            if not step.id:
                self._log("SKIP", "unnamed step (id=None) skipped -- a CLI "
                                  "override could not be matched to a step")
                continue
            if self._pre_step(step, ctx):
                continue

            req_succeeded = step.run_if_succeeded
            req_failed    = step.run_if_failed
            if req_succeeded and req_succeeded not in ctx["completed_steps"]:
                self._log("SKIP", f"{step.id}: skipped "
                                  f"('{req_succeeded}' has not succeeded)")
                ctx["failed_steps"].add(step.id)
                continue
            if req_failed and req_failed not in ctx["failed_steps"]:
                self._log("SKIP", f"{step.id}: skipped "
                                  f"('{req_failed}' has not failed)")
                continue

            program = step.program
            if self._registry.get(program) is None:
                self._log("ERROR",
                          f"{step.id}: no backend registered for program '{program}'")
                return True

            result  = self._invoke(step, ctx)
            status  = result.get("status", "completed")
            message = result.get("message", status)
            line    = f"{step.id}: {message}"

            if status == "completed":
                self._log("RUN", line)
                ctx["last_by_program"][program] = step.id
                ctx["history_by_program"].setdefault(program, []).append(step.id)
                ctx.update(result.get("ctx_updates", {}))
                ctx["completed_steps"].add(step.id)
            elif status == "skipped":
                self._log("SKIP", line)
                ctx["last_by_program"][program] = step.id
                ctx["history_by_program"].setdefault(program, []).append(step.id)
                ctx.update(result.get("ctx_updates", {}))
                ctx["failed_steps"].add(step.id)
            elif status == "error":
                self._log("ERROR", line)
                ctx["failed_steps"].add(step.id)
                if result.get("fatal", False):
                    self._on_fatal(step, ctx, result)
                    return True
        return False
