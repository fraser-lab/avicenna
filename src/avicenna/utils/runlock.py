"""One run at a time per project directory.

Two submissions into one project directory do not overwrite each other, which
would at least be obvious.  The runner sees the output folder a previous run
created and skips that dataset, so the second run quietly does a fraction of the
work and reports itself finished -- a result that looks like a completed run and
is not one.  The two runs also read-modify-write info.json in parallel, so one
of them loses its statistics.

Shape
-----
A lock file at the top of the project directory, named so that a leftover one is
findable with ``ls`` rather than hidden.  It is created with ``O_CREAT|O_EXCL``,
the one exclusive primitive that behaves the same way over NFS, and it records
who holds it: pipeline, time, user, host, pid, command line, and the SGE job ids
once qsub has returned them.

Deciding whether a lock is stale
--------------------------------
The file cannot answer that question -- a lock outlives the process that made it
whenever a job is killed, and a lock that needs manual cleanup after every qdel
is the first thing anyone turns off.  The scheduler can: if the recorded jobs
are still known to ``qstat`` the run is real, and if they are gone the lock is
stale and is cleared automatically.  A lock claimed before qsub returned has no
job ids to check, so it falls back to the pid, which is only meaningful on the
host that recorded it.  When neither can be established -- no ``qstat``, or a
lock written by a host we cannot ask about -- the answer is UNKNOWN and the lock
stands, because guessing "stale" at the wrong moment is what lets two runs
collide.

When it can be overridden
-------------------------
A held lock prints who holds it and refuses to start.  ``--ignore_lock`` takes
over a lock whose holder is UNKNOWN: the user may have a reason the lock cannot
see, and a guess that cannot be overridden blocks real work.  A holder confirmed
ALIVE is not a guess, and two live runs in one directory produce a result that
looks complete, so that refusal stands.
"""

import getpass
import json
import os
import socket
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime

FILENAME = "avicenna_run.lock"

# Bounded so two processes clearing the same stale lock cannot spin.
_MAX_ATTEMPTS = 3

ALIVE = "alive"
GONE = "gone"
UNKNOWN = "unknown"


class RunLockHeld(Exception):
    """Another run holds the lock.  Carries enough to name the holder."""

    def __init__(self, path, info, state, detail):
        self.path = str(path)
        self.info = info or {}
        self.state = state
        self.detail = detail
        super().__init__(self.message())

    def message(self):
        lines = [f"a run is already using this project directory ({self.detail})",
                 f"  lock file: {self.path}"]
        lines.extend(f"  {line}" for line in describe(self.info))
        if self.active:
            lines.append("Wait for it to finish. --ignore_lock does not apply "
                         "while its jobs are running.")
        else:
            lines.append("Wait for it to finish, or re-run with --ignore_lock to "
                         "take the lock over.")
        return "\n".join(lines)

    @property
    def active(self):
        """The holder is confirmed to be running, not merely unaccounted for."""
        return self.state == ALIVE


def describe(info):
    """Human-readable holder lines, for a refusal or a takeover warning."""
    if not info:
        return ["contents unreadable"]
    jobs = info.get("job_ids") or []
    lines = [f"pipeline:  {info.get('pipeline', 'unknown')}",
             f"started:   {info.get('started', 'unknown')}",
             f"user:      {info.get('user', 'unknown')} on "
             f"{info.get('host', 'unknown')} (pid {info.get('pid', 'unknown')})",
             f"jobs:      {', '.join(str(j) for j in jobs) if jobs else 'not submitted yet'}"]
    if info.get("command"):
        lines.append(f"command:   {info['command']}")
    return lines


def job_alive(job_id):
    """True, False, or None when the scheduler cannot be asked.

    Separated out so a test can replace it, and so the one scheduler-specific
    call in this module has a single home.
    """
    try:
        result = subprocess.run(["qstat", "-j", str(job_id)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
    except OSError:
        return None                      # no qstat on this host
    if result.returncode == 0:
        return True
    text = (result.stdout or "").lower()
    # Any other failure (qmaster unreachable, a permissions problem) is not
    # evidence the job finished.
    if "do not exist" in text or "does not exist" in text:
        return False
    return None


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True                      # exists, owned by someone else
    return True


def _host():
    return socket.gethostname()


def _user():
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def holder_state(info):
    """Return ``(ALIVE | GONE | UNKNOWN, why)`` for the holder of a lock."""
    if not info:
        return UNKNOWN, "the lock file could not be read"
    jobs = [j for j in (info.get("job_ids") or []) if j]
    if jobs:
        verdicts = [job_alive(job) for job in jobs]
        if any(verdict is True for verdict in verdicts):
            return ALIVE, "its jobs are still on the cluster"
        if all(verdict is False for verdict in verdicts):
            return GONE, "its jobs are no longer on the cluster"
        return UNKNOWN, "the scheduler could not be queried"
    # Claimed but never got as far as qsub.  The pid only means something where
    # it was recorded.
    if info.get("host") != _host():
        return UNKNOWN, f"it recorded no jobs and belongs to host {info.get('host')}"
    if _pid_alive(info.get("pid")):
        return ALIVE, "its process is still running"
    return GONE, "it recorded no jobs and its process is gone"


def read(path):
    """Return the lock contents, or None if it is absent or unreadable."""
    try:
        with open(path) as handle:
            info = json.load(handle)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _write_over(path, info):
    """Replace the contents of a lock we already hold, atomically.

    A plain rewrite would leave the file briefly truncated, and a truncated lock
    reads as unreadable, which is the one state this module cannot resolve on
    its own.
    """
    directory = os.path.dirname(path) or "."
    handle = tempfile.NamedTemporaryFile("w", dir=directory, prefix=".runlock-",
                                         delete=False)
    try:
        with handle:
            json.dump(info, handle, indent=2)
            handle.write("\n")
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


class RunLock:
    """The project directory's run lock.

    Acquired by the submitting CLI, released once the cluster work is finished
    by the sentinel (or, for analysis, by the pandda job itself) -- a different
    process on a different host, which is why release matches on the recorded
    SGE job id rather than on a pid.
    """

    def __init__(self, project_dir, pipeline, command=None):
        self.project_dir = os.path.abspath(str(project_dir))
        self.pipeline = pipeline
        self.command = command if command is not None else " ".join(sys.argv)
        self.token = uuid.uuid4().hex
        self.held = False
        # Jobs this run submitted.  Non-empty means the lock has been handed to
        # the cluster and the submitting process must no longer release it.
        self.jobs = []

    @property
    def path(self):
        return os.path.join(self.project_dir, FILENAME)

    def _payload(self, job_ids=None):
        return {"pipeline": self.pipeline,
                "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "user": _user(),
                "host": _host(),
                "pid": os.getpid(),
                "command": self.command,
                "job_ids": list(job_ids or []),
                "token": self.token}

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    def acquire(self, ignore=False):
        """Claim the lock.

        Raises ``RunLockHeld`` if another run holds it.  *ignore* takes over a
        lock whose holder cannot be accounted for, never one confirmed alive.
        Returns False, with a warning, if the lock file cannot be created at
        all: that is an environment problem, and refusing to run over it would
        stop work the lock exists only to protect.
        """
        try:
            os.makedirs(self.project_dir, exist_ok=True)
        except OSError as exc:
            print(f"WARNING: could not create {self.project_dir}: {exc}")
            return False

        for _ in range(_MAX_ATTEMPTS):
            try:
                handle = os.open(self.path,
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if not self._clear_if_free(ignore):
                    return False
                continue
            except OSError as exc:
                print(f"WARNING: could not create {self.path}: {exc}")
                print("WARNING: running without a lock; a second run in this "
                      "directory will not be detected.")
                return False
            with os.fdopen(handle, "w") as fh:
                json.dump(self._payload(), fh, indent=2)
                fh.write("\n")
            self.held = True
            return True

        print(f"WARNING: {self.path} keeps reappearing; running without a lock.")
        return False

    def _clear_if_free(self, ignore):
        """Remove a lock that is stale or being taken over.  True = try again."""
        info = read(self.path)
        state, detail = holder_state(info)
        if state == GONE:
            print(f"Clearing a stale run lock ({detail}): {self.path}")
            for line in describe(info):
                print(f"  {line}")
        elif ignore and state != ALIVE:
            # Only a holder that cannot be accounted for may be taken over: two
            # live runs in one directory produce a result that looks complete.
            print(f"WARNING: taking over a run lock held by another run ({detail}):")
            print(f"  lock file: {self.path}")
            for line in describe(info):
                print(f"  {line}")
        else:
            raise RunLockHeld(self.path, info, state, detail)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass                         # someone else cleared it first
        except OSError as exc:
            print(f"WARNING: could not remove {self.path}: {exc}")
            return False
        return True

    def record_jobs(self, *job_ids):
        """Add the submitted job ids, so staleness can be decided by ``qstat``.

        Called as soon as qsub returns: until it is, the lock can only be judged
        by its pid, which no other host can check.
        """
        jobs = [str(job) for job in job_ids if job]
        if not jobs:
            return False
        self.jobs = list(dict.fromkeys(self.jobs + jobs))
        info = read(self.path)
        if not info or info.get("token") != self.token:
            print(f"WARNING: not recording job ids in {self.path}: it is no "
                  "longer the lock this run created.")
            return False
        info["job_ids"] = list(dict.fromkeys(list(info.get("job_ids") or []) + jobs))
        try:
            _write_over(self.path, info)
        except OSError as exc:
            print(f"WARNING: could not update {self.path}: {exc}")
            return False
        return True

    def release(self):
        """Release a lock this process still owns.

        Used when submission produced no job id, so no sentinel exists to do it.
        A lock whose jobs are on the cluster belongs to the sentinel now, and a
        lock that belongs to a later run is left alone.
        """
        if not self.held or self.jobs:
            return False
        info = read(self.path)
        if info is not None and info.get("token") != self.token:
            print(f"WARNING: leaving {self.path} alone: it belongs to another run.")
            return False
        self.held = False
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            print(f"WARNING: could not remove {self.path}: {exc}")
            return False
        return True

    # ------------------------------------------------------------------
    # Release from the sentinel (a different process, on a compute node)
    # ------------------------------------------------------------------

    @classmethod
    def release_for(cls, project_dir, job_id):
        """Release the lock if *job_id* is one of the jobs that holds it.

        The sentinel knows its own SGE job id from the environment; matching on
        it is what keeps a sentinel from releasing a lock that a later run has
        since taken.
        """
        if not job_id:
            return False
        path = os.path.join(os.path.abspath(str(project_dir)), FILENAME)
        info = read(path)
        if not info:
            return False
        if str(job_id) not in [str(job) for job in (info.get("job_ids") or [])]:
            return False
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True
