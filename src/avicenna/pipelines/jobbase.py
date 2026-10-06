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

Run-time limit
--------------
A job killed at its hard run-time limit cannot log anything, so its last
pipeline.log line stays non-terminal and the sentinel waits on it forever.  A
job submitted with a soft limit (every job script ``pipelines.sge`` writes) is
warned with ``SIGUSR1`` first.  ``RuntimeLimited`` turns that warning into the
job's own failure report, then exits, before the hard kill arrives.
"""

import contextlib
import functools
import os
import signal
import sys
import threading
import time
from pathlib import Path


#: Signal SGE sends when a job passes its soft run-time limit (``s_rt``).
RUNTIME_WARNING_SIGNAL = signal.SIGUSR1


class RuntimeLimitReached(BaseException):
    """Raised in the main thread when the run-time warning arrives.

    A BaseException, so a backend's ``except Exception`` cannot swallow it, and
    so ``subprocess.run`` kills the running program on the way out.
    """


class RuntimeLimited:
    """Mixin: a subclass's ``run()`` always runs under the run-time guard.

    Any ``run`` a subclass defines is wrapped when the class is created, so a
    new job type is covered without opting in.  On the warning the guard calls
    ``_on_runtime_limit()`` -- the subclass reports the failure there -- and
    exits with status 1.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        run = cls.__dict__.get("run")
        if run is None or getattr(run, "_runtime_limited", False):
            return

        @functools.wraps(run)
        def guarded(self, *args, **kw):
            with self._runtime_limit_guard():
                return run(self, *args, **kw)

        guarded._runtime_limited = True
        cls.run = guarded

    def _on_runtime_limit(self):
        """Report that the job is stopping at its run-time limit."""

    @contextlib.contextmanager
    def _runtime_limit_guard(self):
        # A signal handler can only be installed from the main thread; a nested
        # run() (a subclass calling super().run()) is already guarded.
        if (threading.current_thread() is not threading.main_thread()
                or getattr(self, "_runtime_guard_active", False)):
            yield
            return
        fired = []

        def _handler(signum, frame):
            # The warning can arrive more than once (to the process group and
            # forwarded by the job script); only the first one counts.
            if fired:
                return
            fired.append(signum)
            signal.signal(signum, signal.SIG_IGN)
            raise RuntimeLimitReached()

        previous = signal.signal(RUNTIME_WARNING_SIGNAL, _handler)
        self._runtime_guard_active = True
        try:
            yield
        except RuntimeLimitReached:
            self._on_runtime_limit()
            raise SystemExit(1)
        finally:
            self._runtime_guard_active = False
            signal.signal(RUNTIME_WARNING_SIGNAL, previous)


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


class SubPipelineJob(RuntimeLimited):
    """Mixin providing the progress-based stall watchdog (and, through
    ``RuntimeLimited``, the run-time guard).

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

    # Step being executed, so a run-time stop can name where it happened.
    _current_step = None

    # -- run-time limit (see RuntimeLimited) ---------------------------------

    def _on_runtime_limit(self):
        """One terminal ERROR line, and the record closed as failed at the
        current step."""
        from avicenna.utils.info import OUTCOME_FAILED
        step = self._current_step
        reason = "run-time limit reached -- job stopped"
        self._log("ERROR", f"{step}: {reason}" if step else reason)
        self._close_record(OUTCOME_FAILED, failed_step=step, reason=reason)

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

    # -- per-process record ---------------------------------------------------
    #
    # Each job keeps its own record of what it did, beside its own logs, and
    # rewrites it after every step.  The stage sentinel gathers those records
    # when the run ends -- several per dataset -- and merges the selected one
    # into the project's info.json.  The values come from the steps themselves,
    # where the dataset, the attempt and the file are all unambiguous, instead
    # of being recovered afterwards by walking the output tree.
    #
    # A sub-pipeline opts in by returning a directory and a key below; one that
    # does not keeps no record and behaves exactly as before.

    _record = None
    _record_warned = False

    def _record_directory(self):
        """Where this job keeps its record, or None to keep none."""
        return None

    def _record_key(self):
        """Dataset tag the record is filed under."""
        return None

    def _record_fields(self):
        """What the job already knows before its first step runs."""
        return {}

    def _open_record(self):
        """Start the record, if this sub-pipeline keeps one."""
        directory, key = self._record_directory(), self._record_key()
        if not directory or not key:
            return None
        try:
            from avicenna.utils.info import ProcessInfo
            self._record = ProcessInfo.start(directory, key,
                                             **self._record_fields())
        except Exception as exc:
            self._record = None
            self._log("WARN", f"could not open the process record: {exc}")
            return None
        self._save_record()
        return self._record

    def _save_record(self):
        """Persist the record.

        A record is metadata and never a precondition, so a failure warns once
        and the job carries on: a dataset whose record never arrived is a state
        the sentinel can already see and name, whereas a dataset killed over
        its metadata is real work lost.
        """
        if self._record is None:
            return
        try:
            self._record.write()
        except Exception as exc:
            if not self._record_warned:
                self._record_warned = True
                self._log("WARN", f"could not write {self._record.path}: {exc}")

    def _note_step(self, step, result, seconds):
        if self._record is None:
            return
        try:
            self._record.note_step(step.id, program=step.program,
                                   status=result.get("status", "completed"),
                                   message=result.get("message"),
                                   seconds=seconds)
        except Exception:
            return
        self._save_record()

    def _record_update(self, **fields):
        """Merge fields into the record without touching how it ended."""
        if self._record is None:
            return
        try:
            self._record.update(self._record.key, **fields)
        except Exception:
            return
        self._save_record()

    def _close_record(self, outcome, failed_step=None, reason=None, **fields):
        """Set how the job ended, with any final fields, and write."""
        if self._record is None:
            return
        try:
            if fields:
                self._record.update(self._record.key, **fields)
            self._record.set_outcome(outcome, failed_step=failed_step,
                                     reason=reason)
        except Exception as exc:
            self._log("WARN", f"could not finalise the process record: {exc}")
            return
        self._save_record()

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

            started = time.time()
            self._current_step = step.id
            result = self._invoke(step, ctx)
            self._current_step = None
            status  = result.get("status", "completed")
            message = result.get("message", status)
            line    = f"{step.id}: {message}"

            # Noted before the branches below so the record and the log line
            # always describe the same step in the same terms.  Only steps that
            # actually ran are noted: a step skipped by a guard is a
            # consequence of an earlier one, and the log already carries it.
            self._note_step(step, result, time.time() - started)

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
                    # Closed before the hook, which may submit further jobs and
                    # has its own ways to fail.
                    from avicenna.utils.info import OUTCOME_FAILED
                    self._close_record(OUTCOME_FAILED, failed_step=step.id,
                                       reason=message)
                    self._on_fatal(step, ctx, result)
                    return True
        return False
