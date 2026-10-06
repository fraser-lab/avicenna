"""Tests for the project-directory run lock.

The test worth having is not that the lock excludes a second run -- that fails
loudly.  It is the staleness rule: too eager and two runs collide in one
directory, too cautious and every killed job leaves a lock that has to be
removed by hand, which is how a lock stops being used at all.  So most of what
is below is about which of ALIVE / GONE / UNKNOWN a given leftover produces, and
about who is allowed to remove a lock they did not create.
"""

import json
import os
import subprocess
import sys

import pytest

from avicenna.utils import runlock
from avicenna.utils.runlock import (ALIVE, FILENAME, GONE, UNKNOWN, RunLock,
                                    RunLockHeld)


def lock_file(tmp_path):
    return tmp_path / FILENAME


def contents(tmp_path):
    return json.loads(lock_file(tmp_path).read_text())


def write_lock(tmp_path, **fields):
    """A lock left behind by some other run."""
    info = {"pipeline": "processing",
            "started": "2026-09-16 09:00:00",
            "user": "someone",
            "host": runlock._host(),
            "pid": os.getpid(),
            "command": "vic.process -d /data",
            "job_ids": [],
            "token": "not-ours"}
    info.update(fields)
    lock_file(tmp_path).write_text(json.dumps(info, indent=2))
    return info


def dead_pid():
    """A pid that has certainly exited."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@pytest.fixture
def no_scheduler(monkeypatch):
    """Default for tests that are not about jobs: qstat cannot be reached."""
    monkeypatch.setattr(runlock, "job_alive", lambda job_id: None)


# --------------------------------------------------------------------------- #
# Acquire and release
# --------------------------------------------------------------------------- #

def test_acquire_writes_a_visible_lock_and_release_removes_it(tmp_path):
    lock = RunLock(tmp_path, "processing", command="vic.process -d /data")
    assert lock.acquire() is True
    assert lock_file(tmp_path).is_file()

    info = contents(tmp_path)
    assert info["pipeline"] == "processing"
    assert info["pid"] == os.getpid()
    assert info["host"] == runlock._host()
    assert info["command"] == "vic.process -d /data"
    assert info["job_ids"] == []

    assert lock.release() is True
    assert not lock_file(tmp_path).exists()


def test_acquire_creates_a_project_directory_that_does_not_exist_yet(tmp_path):
    target = tmp_path / "new_project"
    assert RunLock(target, "processing").acquire() is True
    assert (target / FILENAME).is_file()


def test_recorded_job_ids_land_in_the_lock(tmp_path):
    lock = RunLock(tmp_path, "processing")
    lock.acquire()
    assert lock.record_jobs("4242", None, "4243") is True
    assert contents(tmp_path)["job_ids"] == ["4242", "4243"]
    # Recording twice must not duplicate: the sentinel id arrives separately.
    lock.record_jobs("4243", "4244")
    assert contents(tmp_path)["job_ids"] == ["4242", "4243", "4244"]


def test_a_second_run_is_refused_and_told_who_holds_the_lock(tmp_path, no_scheduler):
    first = RunLock(tmp_path, "processing", command="vic.process -d /data")
    first.acquire()

    with pytest.raises(RunLockHeld) as excinfo:
        RunLock(tmp_path, "processing").acquire()

    message = str(excinfo.value)
    assert str(lock_file(tmp_path)) in message
    assert "processing" in message
    assert str(os.getpid()) in message
    assert "vic.process -d /data" in message
    assert "--ignore_lock" in message
    assert lock_file(tmp_path).is_file()       # refusal leaves the lock alone


def test_ignore_lock_takes_over_a_lock_it_cannot_account_for(tmp_path,
                                                            no_scheduler, capsys):
    write_lock(tmp_path, pipeline="refine", host="another-host")

    taker = RunLock(tmp_path, "processing")
    assert taker.acquire(ignore=True) is True
    assert contents(tmp_path)["token"] == taker.token
    assert "taking over" in capsys.readouterr().out


def test_ignore_lock_takes_over_when_the_scheduler_cannot_be_asked(tmp_path,
                                                                  no_scheduler):
    write_lock(tmp_path, job_ids=["4242"])

    taker = RunLock(tmp_path, "processing")
    assert taker.acquire(ignore=True) is True
    assert contents(tmp_path)["token"] == taker.token


def test_ignore_lock_does_not_override_jobs_that_are_running(tmp_path, monkeypatch):
    """Two live runs in one directory give a result that looks complete, so
    this one is not the user's call to override."""
    monkeypatch.setattr(runlock, "job_alive", lambda job_id: True)
    info = write_lock(tmp_path, job_ids=["4242"])

    with pytest.raises(RunLockHeld) as excinfo:
        RunLock(tmp_path, "processing").acquire(ignore=True)

    assert excinfo.value.active
    assert "does not apply" in str(excinfo.value)
    assert contents(tmp_path)["token"] == info["token"]   # left alone


def test_ignore_lock_does_not_override_a_run_still_submitting(tmp_path,
                                                              no_scheduler):
    """Before qsub returns, the holder's own process is the evidence."""
    write_lock(tmp_path)                    # this host, a live pid, no jobs

    with pytest.raises(RunLockHeld) as excinfo:
        RunLock(tmp_path, "processing").acquire(ignore=True)
    assert excinfo.value.active


def test_a_refusal_it_cannot_account_for_offers_the_override(tmp_path, no_scheduler):
    write_lock(tmp_path, job_ids=["4242"])

    with pytest.raises(RunLockHeld) as excinfo:
        RunLock(tmp_path, "processing").acquire()

    assert not excinfo.value.active
    assert "re-run with --ignore_lock" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #

def test_a_lock_whose_jobs_are_gone_is_stale(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runlock, "job_alive", lambda job_id: False)
    write_lock(tmp_path, job_ids=["4242", "4243"])

    lock = RunLock(tmp_path, "processing")
    assert lock.acquire() is True
    assert contents(tmp_path)["token"] == lock.token
    assert "stale" in capsys.readouterr().out


def test_a_lock_with_one_live_job_is_held(tmp_path, monkeypatch):
    monkeypatch.setattr(runlock, "job_alive", lambda job_id: job_id == "4243")
    write_lock(tmp_path, job_ids=["4242", "4243"])

    with pytest.raises(RunLockHeld) as excinfo:
        RunLock(tmp_path, "processing").acquire()
    assert excinfo.value.state == ALIVE


def test_an_unreachable_scheduler_is_never_a_verdict(tmp_path, monkeypatch):
    """UNKNOWN keeps the lock: guessing "stale" is what lets two runs collide."""
    monkeypatch.setattr(runlock, "job_alive", lambda job_id: None)
    write_lock(tmp_path, job_ids=["4242"])

    with pytest.raises(RunLockHeld) as excinfo:
        RunLock(tmp_path, "processing").acquire()
    assert excinfo.value.state == UNKNOWN


def test_a_lock_with_no_jobs_falls_back_to_its_pid(tmp_path):
    """The window between claiming the lock and qsub returning."""
    assert runlock._pid_alive(os.getpid()) is True

    write_lock(tmp_path, pid=dead_pid())
    state, _ = runlock.holder_state(runlock.read(lock_file(tmp_path)))
    assert state == GONE

    write_lock(tmp_path, pid=os.getpid())
    state, _ = runlock.holder_state(runlock.read(lock_file(tmp_path)))
    assert state == ALIVE


def test_a_pid_on_another_host_is_unknown_not_stale(tmp_path):
    """A pid means nothing anywhere but the machine that recorded it."""
    write_lock(tmp_path, host="some-other-host", pid=dead_pid())
    state, _ = runlock.holder_state(runlock.read(lock_file(tmp_path)))
    assert state == UNKNOWN


def test_an_unreadable_lock_is_held_not_cleared(tmp_path):
    lock_file(tmp_path).write_text("{ this is not json")

    with pytest.raises(RunLockHeld) as excinfo:
        RunLock(tmp_path, "processing").acquire()
    assert excinfo.value.state == UNKNOWN
    assert lock_file(tmp_path).is_file()


# --------------------------------------------------------------------------- #
# Releasing something that is no longer ours
# --------------------------------------------------------------------------- #

def test_release_leaves_a_lock_that_belongs_to_a_later_run(tmp_path, capsys):
    lock = RunLock(tmp_path, "processing")
    lock.acquire()
    write_lock(tmp_path, token="a-later-run")       # taken over meanwhile

    assert lock.release() is False
    assert lock_file(tmp_path).is_file()
    assert "another run" in capsys.readouterr().out


def test_release_leaves_a_lock_the_cluster_now_holds(tmp_path):
    """Once jobs are submitted the sentinel owns the release, not the CLI."""
    lock = RunLock(tmp_path, "processing")
    lock.acquire()
    lock.record_jobs("4242")

    assert lock.release() is False
    assert lock_file(tmp_path).is_file()


def test_release_for_matches_the_sentinels_own_job_id(tmp_path):
    lock = RunLock(tmp_path, "processing")
    lock.acquire()
    lock.record_jobs("4242", "4243")

    assert RunLock.release_for(tmp_path, "9999") is False   # a later run's lock
    assert RunLock.release_for(tmp_path, None) is False
    assert lock_file(tmp_path).is_file()

    assert RunLock.release_for(tmp_path, 4243) is True      # ids may arrive as int
    assert not lock_file(tmp_path).exists()
    assert RunLock.release_for(tmp_path, "4243") is False   # nothing left to do


# --------------------------------------------------------------------------- #
# The scheduler probe itself
# --------------------------------------------------------------------------- #

def test_job_alive_reports_unknown_when_there_is_no_qstat(monkeypatch):
    def no_qstat(*args, **kwargs):
        raise FileNotFoundError("qstat")
    monkeypatch.setattr(runlock.subprocess, "run", no_qstat)
    assert runlock.job_alive("4242") is None


@pytest.mark.parametrize("returncode, output, expected", [
    (0, "job_number: 4242", True),
    (1, "Following jobs do not exist:\n4242", False),
    (1, "error: commlib error: got select error", None),
])
def test_job_alive_reads_qstat(monkeypatch, returncode, output, expected):
    class Result:
        pass

    def fake_run(*args, **kwargs):
        result = Result()
        result.returncode = returncode
        result.stdout = output
        return result

    monkeypatch.setattr(runlock.subprocess, "run", fake_run)
    assert runlock.job_alive("4242") is expected
