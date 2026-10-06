"""What an output folder already holds before a new run starts in it.

The survey decides whether a run is refused, resumed or started, and on resume
which datasets are deleted and redone.  Calling a frayed dataset done leaves it
unfinished for good; calling a done dataset frayed throws away a finished
result.  So each rule is pinned, and so is the survey's promise not to touch
anything: a refused run must leave the folder as it found it.
"""

import os

import pytest

from avicenna.pipelines.survey import (DONE, FRAYED, RUN_COMPLETE,
                                       RUN_INCOMPLETE, RUN_NONE, UNTOUCHED,
                                       classify, survey_run)
from avicenna.utils.info import (OUTCOME_COMPLETED, OUTCOME_FAILED,
                                 ProcessInfo)
from avicenna.utils.utils import dataset_tag

from tests.test_discovery import cbf_series, pin_line

TAG = "als_20251206_FRS005_10_1"
OTHER = "als_20251206_FRS005_11_1"


def job_dir(process_dir, key=TAG, form="1_P43212_refined", index="index_1_900"):
    path = process_dir / key / form / index
    path.mkdir(parents=True, exist_ok=True)
    return path


def record(directory, key=TAG, outcome=OUTCOME_COMPLETED, triggered_forced=False):
    """One job's record, as a worker leaves it."""
    info = ProcessInfo.start(directory, key)
    if outcome == OUTCOME_FAILED:
        info.set_outcome(outcome, failed_step="1_xds", reason="indexing failed")
    elif outcome is not None:
        info.set_outcome(outcome)
    if triggered_forced:
        info.update(key, triggered_forced=True)
    info.write()
    return info


def reject(process_dir, key):
    rejected = process_dir / ".rejected"
    rejected.mkdir(parents=True, exist_ok=True)
    (rejected / f"{key}.txt").write_text("partial dataset\n")


def class_of(process_dir, key=TAG):
    return classify([key], process_dir).classes[key]


# --------------------------------------------------------------------------- #
# Done                                                                         #
# --------------------------------------------------------------------------- #

def test_a_completed_job_is_done(tmp_path):
    record(job_dir(tmp_path))
    assert class_of(tmp_path) == DONE


def test_a_failed_job_is_done(tmp_path):
    """Failing on its data is an answer; rerunning it gives the same one."""
    record(job_dir(tmp_path), outcome=OUTCOME_FAILED)
    assert class_of(tmp_path) == DONE


def test_a_dataset_rejected_before_submission_is_done(tmp_path):
    reject(tmp_path, TAG)
    assert class_of(tmp_path) == DONE


def test_every_job_of_a_dataset_has_to_be_finished(tmp_path):
    record(job_dir(tmp_path, form="1_P43212_refined"))
    record(job_dir(tmp_path, form="2_P41212_refined"))
    assert class_of(tmp_path) == DONE


def test_a_finished_forced_retry_finishes_the_job(tmp_path):
    parent = job_dir(tmp_path)
    record(parent, outcome=OUTCOME_FAILED, triggered_forced=True)
    retry = parent / "1_P4_force"
    retry.mkdir()
    record(retry)
    assert class_of(tmp_path) == DONE


# --------------------------------------------------------------------------- #
# Frayed                                                                       #
# --------------------------------------------------------------------------- #

def test_a_job_that_left_no_record_is_frayed(tmp_path):
    job_dir(tmp_path)
    assert class_of(tmp_path) == FRAYED


def test_a_job_still_running_is_frayed(tmp_path):
    record(job_dir(tmp_path), outcome=None)
    assert class_of(tmp_path) == FRAYED


def test_an_unreadable_record_is_frayed(tmp_path):
    directory = job_dir(tmp_path)
    (directory / f"{TAG}_info.json").write_text("{ not json")
    assert class_of(tmp_path) == FRAYED


def test_one_unfinished_job_frays_the_whole_dataset(tmp_path):
    record(job_dir(tmp_path, form="1_P43212_refined"))
    job_dir(tmp_path, form="2_P41212_refined")
    assert class_of(tmp_path) == FRAYED


def test_a_handover_to_a_retry_that_never_started_is_frayed(tmp_path):
    record(job_dir(tmp_path), outcome=OUTCOME_FAILED, triggered_forced=True)
    assert class_of(tmp_path) == FRAYED


def test_a_handover_to_a_retry_still_running_is_frayed(tmp_path):
    parent = job_dir(tmp_path)
    record(parent, outcome=OUTCOME_FAILED, triggered_forced=True)
    retry = parent / "1_P4_force"
    retry.mkdir()
    record(retry, outcome=None)
    assert class_of(tmp_path) == FRAYED


def test_a_dataset_folder_with_no_jobs_in_it_is_frayed(tmp_path):
    """Folders are made at submission, before a job runs."""
    (tmp_path / TAG).mkdir()
    assert class_of(tmp_path) == FRAYED


def test_a_folder_from_before_records_existed_is_frayed(tmp_path):
    """Results with no record cannot be vouched for, so they are redone."""
    directory = job_dir(tmp_path)
    (directory / "6_aimless").mkdir()
    (directory / "6_aimless" / "aimless.log").write_text("Overall  InnerShell\n")
    assert class_of(tmp_path) == FRAYED


# --------------------------------------------------------------------------- #
# Untouched, and the run as a whole                                            #
# --------------------------------------------------------------------------- #

def test_a_dataset_with_nothing_in_the_folder_is_untouched(tmp_path):
    assert class_of(tmp_path) == UNTOUCHED


def test_an_empty_folder_is_no_run(tmp_path):
    survey = classify([TAG, OTHER], tmp_path)
    assert survey.state == RUN_NONE
    assert survey.done == 0


def test_every_dataset_done_is_a_complete_run(tmp_path):
    record(job_dir(tmp_path, key=TAG), key=TAG)
    reject(tmp_path, OTHER)
    survey = classify([TAG, OTHER], tmp_path)
    assert survey.state == RUN_COMPLETE
    assert (survey.done, survey.total) == (2, 2)


def test_some_datasets_untouched_is_an_incomplete_run(tmp_path):
    record(job_dir(tmp_path, key=TAG), key=TAG)
    survey = classify([TAG, OTHER], tmp_path)
    assert survey.state == RUN_INCOMPLETE
    assert (survey.done, survey.total) == (1, 2)
    assert survey.frayed == []


def test_a_frayed_dataset_is_an_incomplete_run_and_is_named(tmp_path):
    record(job_dir(tmp_path, key=TAG), key=TAG)
    job_dir(tmp_path, key=OTHER)
    survey = classify([TAG, OTHER], tmp_path)
    assert survey.state == RUN_INCOMPLETE
    assert survey.frayed == [OTHER]


def test_folders_the_input_no_longer_names_are_not_counted(tmp_path):
    job_dir(tmp_path, key=OTHER)                  # frayed, but not asked about
    record(job_dir(tmp_path, key=TAG), key=TAG)
    survey = classify([TAG], tmp_path)
    assert survey.state == RUN_COMPLETE
    assert survey.total == 1


# --------------------------------------------------------------------------- #
# The survey reads and never writes                                            #
# --------------------------------------------------------------------------- #

def _snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


def test_the_survey_changes_nothing_in_the_folder(tmp_path):
    record(job_dir(tmp_path, key=TAG), key=TAG)
    damaged = job_dir(tmp_path, key=OTHER)
    (damaged / f"{OTHER}_info.json").write_text("{ not json")
    reject(tmp_path, "als_20251206_FRS005_12_1")
    before = _snapshot(tmp_path)

    classify([TAG, OTHER], tmp_path)

    assert _snapshot(tmp_path) == before


# --------------------------------------------------------------------------- #
# From the input list                                                          #
# --------------------------------------------------------------------------- #

def test_the_input_list_is_expanded_the_way_info_json_is(tmp_path):
    collection = tmp_path / "als_20251206_transfer"
    cbf_series(collection / "FRS005_10", "C6")
    cbf_series(collection / "FRS005_11", "C7")
    process_dir = tmp_path / "processing"
    process_dir.mkdir()
    key = dataset_tag(str(collection), "FRS005_10", 1)
    record(job_dir(process_dir, key=key), key=key)

    survey = survey_run([pin_line(collection, "FRS005_10"),
                         pin_line(collection, "FRS005_11")], process_dir)

    assert survey.total == 2
    assert survey.classes[key] == DONE
    assert survey.state == RUN_INCOMPLETE
