"""A job warned that its run-time limit is near records why it stopped.

The scheduler's hard kill leaves nothing behind, so the sentinel would wait on
a job that still looks like it is running.  The soft limit's warning is turned
into a terminal ERROR line and a failed record instead.
"""

import os
import signal
import subprocess
import time

import pytest

from avicenna.pipelines.jobbase import RUNTIME_WARNING_SIGNAL, RuntimeLimited
from avicenna.pipelines.refinement import RefinementPipelineInterpreter
from avicenna.pipelines.refinement_runner import RunQRefine
from avicenna.pipelines.sge import soft_limit
from avicenna.utils.info import OUTCOME_FAILED, ProcessInfo
from avicenna.utils.utils import QRefineWatcher

TAG = "als_20251206_FRS005_10_1"


def _interpreter(job_dir, monkeypatch):
    params = {"dataset_tag": TAG, "pin": TAG, "suffix": "hoh",
              "mtz": "/project/processing/x/XDS_ASCII_ctruncate.mtz",
              "job_dir": str(job_dir), "job_id": TAG}
    interpreter = RefinementPipelineInterpreter(params)

    def _warned(step, ctx):
        os.kill(os.getpid(), RUNTIME_WARNING_SIGNAL)
        time.sleep(5)
        raise AssertionError("the warning did not stop the step")

    monkeypatch.setattr(interpreter, "_invoke", _warned)
    return interpreter


def test_a_warned_job_logs_a_terminal_failure_and_exits(tmp_path, monkeypatch):
    job_dir = tmp_path / "qrefine" / TAG
    job_dir.mkdir(parents=True)
    interpreter = _interpreter(job_dir, monkeypatch)

    with pytest.raises(SystemExit) as stop:
        interpreter.run()

    assert stop.value.code == 1
    last = (job_dir / "pipeline.log").read_text().splitlines()[-1]
    assert last.split(None, 3)[2] == "ERROR"
    assert "run-time limit" in last
    watcher = QRefineWatcher.__new__(QRefineWatcher)
    assert watcher._read_pipeline_log(job_dir / "pipeline.log")[0] == "FAILED"


def test_a_warned_job_records_the_step_it_was_in(tmp_path, monkeypatch):
    job_dir = tmp_path / "qrefine" / TAG
    job_dir.mkdir(parents=True)
    interpreter = _interpreter(job_dir, monkeypatch)

    with pytest.raises(SystemExit):
        interpreter.run()

    record = ProcessInfo.load(ProcessInfo.path_in(job_dir, TAG))[TAG]
    assert record["outcome"] == OUTCOME_FAILED
    assert record["failed_step"]
    assert "run-time limit" in record["failure_reason"]


def test_the_guard_restores_the_previous_handler(tmp_path, monkeypatch):
    job_dir = tmp_path / "qrefine" / TAG
    job_dir.mkdir(parents=True)
    before = signal.getsignal(RUNTIME_WARNING_SIGNAL)

    with pytest.raises(SystemExit):
        _interpreter(job_dir, monkeypatch).run()

    assert signal.getsignal(RUNTIME_WARNING_SIGNAL) == before


@pytest.mark.parametrize("h_rt, s_rt", [("02:00:00", "01:55:00"),
                                         ("24:00:00", "23:55:00"),
                                         ("00:05:00", "00:04:30")])
def test_the_soft_limit_falls_before_the_hard_one(h_rt, s_rt):
    assert soft_limit(h_rt) == s_rt


def _limits(text):
    return {line.split("=")[0].split()[-1]: line.split("=")[1]
            for line in text.splitlines()
            if line.startswith("#$ -l ") and "_rt=" in line}


def test_the_refine_array_has_both_limits(tmp_path):
    runner = RunQRefine(tmp_path)
    text = runner._write_qsub_submit(tmp_path / "list.txt", 1).read_text()

    assert _limits(text) == {"h_rt": runner.H_RT, "s_rt": soft_limit(runner.H_RT)}


def test_the_processing_array_has_both_limits(tmp_path, monkeypatch):
    import avicenna.pipelines.processing_runner as processing_runner
    monkeypatch.setattr(processing_runner, "qsub_terse", lambda args: "1")
    runner = processing_runner.RunProcess.__new__(processing_runner.RunProcess)
    runner.ncpu = "auto"
    job_list = tmp_path / "list.txt"
    job_list.write_text(f"{tmp_path}\n")
    submit = tmp_path / "submit.sh"

    runner._submit_sge_array(job_list, submit, h_rt="06:00:00")
    text = submit.read_text()

    assert _limits(text) == {"h_rt": "06:00:00", "s_rt": "05:55:00"}
    assert "#$ -l scratch=20G" in text
    assert "./run_pipeline.py &" in text


def test_the_sentinel_has_both_limits(tmp_path, monkeypatch):
    import avicenna.pipelines.sentinel as sentinel
    monkeypatch.setattr(sentinel, "qsub_terse", lambda args: "2")

    sentinel.RefinementSentinel(tmp_path).submit("1")
    text = (tmp_path / "sentinel_refine_submit.sh").read_text()

    limits = _limits(text)
    assert limits["s_rt"] == soft_limit(limits["h_rt"])
    assert "#$ -hold_jid 1" in text


def test_every_interpreter_and_sentinel_is_guarded():
    from avicenna.pipelines.analysis import AnalysisPipelineInterpreter
    from avicenna.pipelines.processing import ProcessingPipelineInterpreter
    from avicenna.pipelines.sentinel import (AnalysisSentinel,
                                             ProcessingSentinel,
                                             RefinementSentinel)
    for cls in (ProcessingPipelineInterpreter, RefinementPipelineInterpreter,
                AnalysisPipelineInterpreter, ProcessingSentinel,
                RefinementSentinel, AnalysisSentinel):
        assert getattr(cls.run, "_runtime_limited", False), cls.__name__


def test_a_new_job_type_is_guarded_without_opting_in():
    calls = []

    class _Job(RuntimeLimited):
        def run(self):
            os.kill(os.getpid(), RUNTIME_WARNING_SIGNAL)
            time.sleep(5)

        def _on_runtime_limit(self):
            calls.append("reported")

    with pytest.raises(SystemExit) as stop:
        _Job().run()

    assert stop.value.code == 1
    assert calls == ["reported"]


def test_a_warned_sentinel_fails_the_run_and_releases_the_lock(tmp_path,
                                                               monkeypatch):
    from avicenna.pipelines.sentinel import RefinementSentinel
    from avicenna.utils.utils import SENTINEL_WAIT_FAILED, sentinel_outcome

    class _Busy:
        def is_running(self):
            os.kill(os.getpid(), RUNTIME_WARNING_SIGNAL)
            time.sleep(5)
            return True

    sentinel = RefinementSentinel(tmp_path)
    released = []
    monkeypatch.setattr(sentinel, "_watcher", lambda: _Busy())
    monkeypatch.setattr(sentinel, "_release_lock", lambda: released.append(1))

    with pytest.raises(SystemExit) as stop:
        sentinel.run()

    assert stop.value.code == 1
    assert released
    assert sentinel_outcome(sentinel.omnibus) == SENTINEL_WAIT_FAILED


def test_the_submit_script_hands_the_warning_to_the_job(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    job = job_dir / "run_job.sh"
    job.write_text(
        "#!/bin/bash\n"
        "trap 'echo warned > warned; exit 3' USR1\n"
        "touch ready\n"
        "while true; do sleep 0.1; done\n"
    )
    job.chmod(0o755)
    qsub_list = tmp_path / "list.txt"
    qsub_list.write_text(f"{job}\n")
    submit = RunQRefine(tmp_path)._write_qsub_submit(qsub_list, 1)

    env = dict(os.environ, SGE_TASK_ID="1")
    proc = subprocess.Popen(["bash", str(submit)], cwd=tmp_path, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        while not (job_dir / "ready").exists():
            assert time.time() < deadline, "job never started"
            time.sleep(0.05)
        # Only the submit script is signalled, as when the scheduler does not
        # signal the whole process group.
        proc.send_signal(signal.SIGUSR1)
        rc = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert (job_dir / "warned").read_text().strip() == "warned"
    assert rc == 3


def test_the_running_program_is_stopped_with_the_job(tmp_path, monkeypatch):
    import threading
    job_dir = tmp_path / "qrefine" / TAG
    job_dir.mkdir(parents=True)
    pid_file = job_dir / "child.pid"
    interpreter = _interpreter(job_dir, monkeypatch)

    def _program(step, ctx):
        threading.Timer(0.5, os.kill,
                        (os.getpid(), RUNTIME_WARNING_SIGNAL)).start()
        subprocess.run(["bash", "-c", f"echo $$ > {pid_file}; sleep 30"])
        raise AssertionError("the warning did not stop the program")

    monkeypatch.setattr(interpreter, "_invoke", _program)
    started = time.time()
    with pytest.raises(SystemExit):
        interpreter.run()

    assert time.time() - started < 10
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
