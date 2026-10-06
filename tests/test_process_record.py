"""The record a worker process keeps of itself, and the step loop that fills it.

The point of these is the failure mode the design exists for: a worker that is
killed -- by the stall watchdog, by ``h_rt``, by ``qdel`` -- must still leave a
record that says how far it got.  So what is tested is not only that a finished
job writes the right thing, but that every intermediate state on disk is
complete and parseable, and that the job survives a record it cannot write.
"""

import json
import types

import pytest

from avicenna.pipelines.jobbase import SubPipelineInterpreter
from avicenna.pipelines.processing_runner import (RunProcess,
                                                  format_aimless_summary)
from avicenna.utils.info import (
    OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_RUNNING, PREV_SUFFIX,
    ProcessInfo, ProjectInfo,
)

TAG = "als_20251206_FRS005_10_1"


# --------------------------------------------------------------------------- #
# ProcessInfo                                                                  #
# --------------------------------------------------------------------------- #

def test_the_file_is_named_for_its_dataset(tmp_path):
    record = ProcessInfo.start(tmp_path, TAG)
    record.write()

    written = tmp_path / f"{TAG}_info.json"
    assert written.is_file()
    assert json.loads(written.read_text())["datasets"][TAG]["outcome"] == OUTCOME_RUNNING


def test_a_gather_pattern_cannot_pick_up_the_project_record(tmp_path):
    ProcessInfo.start(tmp_path, TAG).write()
    ProjectInfo(tmp_path).write()

    from avicenna.utils.info import PROCESS_PATTERN
    found = sorted(p.name for p in tmp_path.glob(PROCESS_PATTERN))
    assert found == [f"{TAG}_info.json"]


def test_the_dataset_is_recoverable_from_the_file_name(tmp_path):
    """A record whose contents were lost can still be attributed."""
    path = tmp_path / f"{TAG}_info.json"
    path.write_text("{ this is not json")

    recovered = ProcessInfo.load(path)
    assert recovered.datasets == {}
    assert recovered.key == TAG
    assert recovered.recovered is not None


def test_a_kill_during_a_write_leaves_the_last_complete_record(tmp_path,
                                                               monkeypatch):
    """The step being written is lost, never the record: the kill abandons the
    temporary and the previous generation stays in place, whole."""
    record = ProcessInfo.start(tmp_path, TAG)
    record.note_step("1_xds", program="xds", status="completed")
    record.write()

    record.note_step("2_aimless", program="aimless", status="completed")

    def killed(fd):
        raise KeyboardInterrupt
    monkeypatch.setattr("avicenna.utils.info.os.fsync", killed)
    with pytest.raises(KeyboardInterrupt):
        record.write()
    monkeypatch.undo()

    on_disk = json.loads((tmp_path / f"{TAG}_info.json").read_text())
    assert [s["id"] for s in on_disk["datasets"][TAG]["steps"]] == ["1_xds"]


def test_a_temporary_left_by_a_hard_kill_is_not_a_record(tmp_path):
    """SIGKILL gives no chance to clean up, so the temporary stays behind; it
    must not be gathered as if it were a record."""
    ProcessInfo.start(tmp_path, TAG).write()
    (tmp_path / f".{TAG}_info.json.abc123.tmp").write_text('{"datasets": {')

    from avicenna.utils.info import PROCESS_PATTERN
    found = sorted(p.name for p in tmp_path.glob(PROCESS_PATTERN))
    assert found == [f"{TAG}_info.json"]


def test_no_previous_generation_is_kept(tmp_path):
    record = ProcessInfo.start(tmp_path, TAG)
    record.write()
    record.note_step("1_xds", program="xds", status="completed")
    record.write()

    assert not (tmp_path / f"{TAG}_info.json{PREV_SUFFIX}").exists()


def test_the_project_record_still_keeps_one(tmp_path):
    """The flag divides the two records; it does not switch the feature off."""
    info = ProjectInfo(tmp_path)
    info.write()
    info.update(TAG, status="imported")
    info.write()

    assert (tmp_path / f"info.json{PREV_SUFFIX}").is_file()


def test_an_unknown_outcome_is_refused(tmp_path):
    record = ProcessInfo.start(tmp_path, TAG)
    with pytest.raises(ValueError):
        record.set_outcome("finished-ish")


def test_the_failing_step_is_its_own_field(tmp_path):
    record = ProcessInfo.start(tmp_path, TAG)
    record.set_outcome(OUTCOME_FAILED, failed_step="4_xds",
                       reason="integration produced no reflections")
    written = record.record

    assert written["failed_step"] == "4_xds"
    assert written["outcome"] == OUTCOME_FAILED
    assert written["finished"]


def test_ending_well_clears_the_failure_fields(tmp_path):
    """A consumer reads both unconditionally, so both are always present."""
    record = ProcessInfo.start(tmp_path, TAG)
    record.set_outcome(OUTCOME_FAILED, failed_step="4_xds", reason="nope")
    record.set_outcome(OUTCOME_COMPLETED)

    assert record.record["failed_step"] is None
    assert record.record["failure_reason"] is None


# --------------------------------------------------------------------------- #
# The step loop                                                                #
# --------------------------------------------------------------------------- #

def _step(step_id, program):
    return types.SimpleNamespace(id=step_id, program=program,
                                 run_if_succeeded=None, run_if_failed=None)


def _ctx():
    return {"last_by_program": {}, "history_by_program": {},
            "completed_steps": set(), "failed_steps": set()}


class _Interpreter(SubPipelineInterpreter):
    """The smallest thing that can run steps: every step returns a canned
    result, and each one snapshots what is on disk at that moment."""

    def __init__(self, directory, results):
        self.params = {}
        self._registry = {"xds": True, "aimless": True}
        self._directory = directory
        self._results = results
        self.lines = []
        self.snapshots = []

    def _log(self, tag, message):
        self.lines.append((tag, message))

    def _record_directory(self):
        return self._directory

    def _record_key(self):
        return TAG

    def _invoke(self, step, ctx):
        return self._results[step.id]

    def _note_step(self, step, result, seconds):
        super()._note_step(step, result, seconds)
        path = self._directory / f"{TAG}_info.json"
        self.snapshots.append(json.loads(path.read_text()) if path.is_file() else None)


def test_every_step_that_ran_is_in_the_record(tmp_path):
    interp = _Interpreter(tmp_path, {
        "1_xds":     {"status": "completed", "message": "indexed"},
        "2_aimless": {"status": "completed", "message": "scaled"},
    })
    interp._open_record()
    fatal = interp._run_steps([_step("1_xds", "xds"),
                               _step("2_aimless", "aimless")], _ctx())

    assert not fatal
    steps = interp._record.record["steps"]
    assert [s["id"] for s in steps] == ["1_xds", "2_aimless"]
    assert [s["program"] for s in steps] == ["xds", "aimless"]
    assert steps[0]["message"] == "indexed"
    assert all(s["seconds"] >= 0 for s in steps)


def test_a_step_skipped_by_a_guard_is_not_recorded(tmp_path):
    """Only steps that ran are noted -- the rest are consequences, and the
    pipeline log carries them."""
    interp = _Interpreter(tmp_path, {"1_xds": {"status": "completed"}})
    interp._open_record()
    later = _step("2_aimless", "aimless")
    later.run_if_succeeded = "never_ran"
    interp._run_steps([_step("1_xds", "xds"), later], _ctx())

    assert [s["id"] for s in interp._record.record["steps"]] == ["1_xds"]


def test_a_killed_worker_leaves_a_complete_record_of_what_finished(tmp_path):
    """Each write lands whole, so every state a kill could freeze is usable."""
    interp = _Interpreter(tmp_path, {
        "1_xds":     {"status": "completed", "message": "indexed"},
        "2_aimless": {"status": "completed", "message": "scaled"},
    })
    interp._open_record()
    interp._run_steps([_step("1_xds", "xds"), _step("2_aimless", "aimless")],
                      _ctx())

    assert len(interp.snapshots) == 2
    for taken, expected in zip(interp.snapshots, (1, 2)):
        record = taken["datasets"][TAG]
        assert len(record["steps"]) == expected
        assert record["outcome"] == OUTCOME_RUNNING   # still going at that point


def test_a_fatal_step_closes_the_record_where_it_stopped(tmp_path):
    interp = _Interpreter(tmp_path, {
        "1_xds": {"status": "completed"},
        "4_xds": {"status": "error", "message": "no reflections", "fatal": True},
        "5_aimless": {"status": "completed"},
    })
    interp._open_record()
    fatal = interp._run_steps([_step("1_xds", "xds"), _step("4_xds", "xds"),
                               _step("5_aimless", "aimless")], _ctx())

    assert fatal
    on_disk = json.loads((tmp_path / f"{TAG}_info.json").read_text())
    record = on_disk["datasets"][TAG]
    assert record["outcome"] == OUTCOME_FAILED
    assert record["failed_step"] == "4_xds"
    assert record["failure_reason"] == "no reflections"
    assert [s["id"] for s in record["steps"]] == ["1_xds", "4_xds"]


def test_a_record_that_cannot_be_written_warns_once_and_the_job_goes_on(tmp_path):
    interp = _Interpreter(tmp_path, {"1_xds": {"status": "completed"},
                                     "2_aimless": {"status": "completed"}})
    interp._open_record()

    def _refuse():
        raise OSError("read-only file system")

    interp._record.write = _refuse
    fatal = interp._run_steps([_step("1_xds", "xds"), _step("2_aimless", "aimless")],
                              _ctx())

    assert not fatal
    assert sum(1 for tag, _ in interp.lines if tag == "WARN") == 1


def test_no_record_is_kept_when_the_sub_pipeline_does_not_ask_for_one(tmp_path):
    """Refinement and analysis are not wired up yet and must be unaffected."""
    interp = _Interpreter(tmp_path, {"1_xds": {"status": "completed"}})
    interp._record_directory = lambda: None
    assert interp._open_record() is None

    interp._run_steps([_step("1_xds", "xds")], _ctx())
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# Aimless statistics, parsed once where the file is unambiguous                #
# --------------------------------------------------------------------------- #

AIMLESS_LOG = """\
$TEXT:Result: $$ $$
                                       Overall  InnerShell  OuterShell
Low resolution limit                     49.86     49.86      1.88
High resolution limit                     1.85      8.27      1.85
Rmerge  (within I+/I-)                   0.062     0.031     0.712
Rpim (within I+/I-)                      0.028     0.014     0.401
Total number of observations            210441      3625      9877
Mn(I) half-set correlation CC(1/2)       0.998     0.999    -0.021
Completeness                              99.8     100.0      98.2
Multiplicity                              12.4      13.1       9.6
Mn(I/sd(I))                               14.2      44.6       1.1
Average unit cell:  78.90  78.90  37.10  90.00  90.00  90.00
Average mosaicity:       0.12
Space group: P 43 21 2
"""


@pytest.fixture
def aimless_log(tmp_path):
    path = tmp_path / "3_aimless.log"
    path.write_text(AIMLESS_LOG)
    return path


def test_each_shell_row_is_kept_whole(aimless_log):
    stats = RunProcess.__new__(RunProcess)._aimless_stats(aimless_log)

    assert stats["completeness"] == ["99.8", "100.0", "98.2"]
    assert stats["multiplicity"] == ["12.4", "13.1", "9.6"]
    assert stats["i_sig_i"] == ["14.2", "44.6", "1.1"]
    assert stats["high_res_limit"] == ["1.85", "8.27", "1.85"]
    assert stats["unit_cell"] == ["78.90", "78.90", "37.10",
                                  "90.00", "90.00", "90.00"]
    assert stats["space_group"] == "P43212"
    assert stats["mosaicity"] == "0.12"


def test_a_negative_outer_shell_correlation_keeps_its_column(aimless_log):
    """Dropping the minus sign would shift every column after it, and the outer
    shell would silently be read from the inner one."""
    stats = RunProcess.__new__(RunProcess)._aimless_stats(aimless_log)

    assert stats["cc_half"] == ["0.998", "0.999", "-0.021"]
    assert stats["cc_half"][-1] == "-0.021"


def test_the_log_line_is_derived_from_the_parsed_values(aimless_log):
    runner = RunProcess.__new__(RunProcess)
    summary = runner._aimless_summary(aimless_log)

    assert summary == format_aimless_summary(runner._aimless_stats(aimless_log))
    for expected in ("res=1.85", "SG=P43212", "comp=99.8", "rpim=0.028"):
        assert expected in summary


def test_a_missing_log_is_not_an_error(tmp_path):
    runner = RunProcess.__new__(RunProcess)
    assert runner._aimless_stats(tmp_path / "absent.log") == {}
    assert runner._aimless_summary(tmp_path / "absent.log") == "completed"


# --------------------------------------------------------------------------- #
# A dataset rejected before submission                                         #
# --------------------------------------------------------------------------- #

def test_a_rejection_is_logged_with_the_reason_it_records(tmp_path, capsys):
    """The printed line reaches processing.log through the CLI's stdout tee;
    the marker is what the gather reads into info.json."""
    runner = RunProcess.__new__(RunProcess)
    runner._output_dir = tmp_path
    reason = "partial dataset (40 images, minimum is 100)"

    runner._record_rejection("als_20251206_FRS005_10_1", reason)

    marker = tmp_path / ".rejected" / "als_20251206_FRS005_10_1.txt"
    assert marker.read_text().strip() == reason
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert out[0].startswith("REJECTED")
    assert out[0].endswith(f"als_20251206_FRS005_10_1: {reason}")


# --------------------------------------------------------------------------- #
# What the processing interpreter puts in its record                           #
# --------------------------------------------------------------------------- #

def _processing_interpreter(tmp_path, subdir, **overrides):
    from avicenna.pipelines.processing import ProcessingPipelineInterpreter
    params = {"pin_id": TAG, "pipeline_subdir": str(subdir),
              "image_prefix": "C6", "image_range": "1-900",
              "data_format": "cbf", "known_sg": "P43212",
              "force_integration": "false",
              "original_image_dir": "/collection/FRS005_10",
              "image_dir": "/scratch/copy/FRS005_10"}
    params.update(overrides)
    runner = types.SimpleNamespace(_output_dir=tmp_path / "processing")
    return ProcessingPipelineInterpreter(params, rp=runner)


def test_a_job_records_the_images_as_collected_not_the_scratch_copy(tmp_path):
    """image_dir is rewritten to scratch when the job starts, and that path
    outlives nothing."""
    subdir = tmp_path / "processing" / TAG / "1_P43212_refined" / "index_1_900"
    fields = _processing_interpreter(tmp_path, subdir)._record_fields()

    assert fields["image_dir"] == "/collection/FRS005_10"
    assert fields["image_prefix"] == "C6"
    assert fields["forced"] is False


def test_a_forced_retry_reports_the_same_dataset_directory(tmp_path):
    """Forced jobs sit one level deeper, and the sentinel groups records by
    dataset -- so the depth must be derived, not assumed."""
    ordinary = tmp_path / "processing" / TAG / "1_P43212_refined" / "index_1_900"
    forced = ordinary / "1_P43212_force"
    fields = _processing_interpreter(
        tmp_path, forced, force_integration="true", sg="P43212",
        cell="78.9 78.9 37.1 90 90 90")._record_fields()

    assert fields["proc_path"] == str(tmp_path / "processing" / TAG)
    assert fields["result_path"] == str(ordinary.parent)
    assert fields["forced"] is True
    assert fields["forced_sg"] == "P43212"


def test_an_unrecognisable_layout_costs_the_paths_not_the_record(tmp_path):
    fields = _processing_interpreter(tmp_path, tmp_path / "elsewhere")._record_fields()

    assert fields["proc_path"] is None
    assert fields["image_prefix"] == "C6"


# --------------------------------------------------------------------------- #
# The merged data file handed to refinement                                    #
# --------------------------------------------------------------------------- #

def _rfree_ctx(subdir, rfree_step="10_rfree"):
    last = {"aimless": "9_aimless"}
    if rfree_step:
        last["rfree"] = rfree_step
    return {"last_by_program": last, "pipeline_subdir": subdir}


def _quiet(interpreter):
    logged = []
    interpreter._log = lambda tag, message: logged.append((tag, message))
    return logged


def test_the_record_names_the_merged_data_file(tmp_path):
    subdir = tmp_path / "processing" / TAG / "1_P43212_refined" / "index_1_900"
    mtz = subdir / "10_rfree" / "XDS_ASCII_ctruncate.mtz"
    mtz.parent.mkdir(parents=True)
    mtz.write_text("")
    interpreter = _processing_interpreter(tmp_path, subdir)
    logged = _quiet(interpreter)

    final_mtz = interpreter._final_mtz(_rfree_ctx(subdir))

    assert final_mtz == str(mtz.resolve())
    assert logged == []


def test_a_missing_merged_data_file_is_left_out_with_a_warning(tmp_path):
    subdir = tmp_path / "processing" / TAG / "1_P43212_refined" / "index_1_900"
    (subdir / "10_rfree").mkdir(parents=True)
    interpreter = _processing_interpreter(tmp_path, subdir)
    logged = _quiet(interpreter)

    assert interpreter._final_mtz(_rfree_ctx(subdir)) is None
    assert [tag for tag, _ in logged] == ["WARN"]


def test_no_rfree_step_means_no_merged_data_file(tmp_path):
    subdir = tmp_path / "processing" / TAG / "1_P43212_refined" / "index_1_900"
    subdir.mkdir(parents=True)
    interpreter = _processing_interpreter(tmp_path, subdir)
    logged = _quiet(interpreter)

    assert interpreter._final_mtz(_rfree_ctx(subdir, rfree_step=None)) is None
    assert [tag for tag, _ in logged] == ["WARN"]
