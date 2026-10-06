"""Gathering per-process records into info.json.

The acceptance criterion is the transfer: what a worker recorded about itself
has to arrive in the project record intact, because that record is what every
later stage reads and nothing re-derives it afterwards.  The rest of these
cover the states a dataset can be in when it left no usable record -- those are
the ones that would otherwise vanish from the run's account of itself and read
as if they had never been collected.
"""

import json

import pytest

from avicenna.pipelines.gather import (ProcessingGather, RunSurvey, collect,
                                       resolve, select)
from avicenna.utils.info import (FAILED_STAGE, OUTCOME_COMPLETED,
                                 OUTCOME_FAILED, OUTCOME_REJECTED,
                                 OUTCOME_RUNNING, OUTCOME_UNPROCESSED,
                                 STAGE_PROCESS, STAGE_REFINE, STATUS_FAILED,
                                 STATUS_PROCESSED, ProcessInfo, ProjectInfo)
from avicenna.utils.terms import ProcessingTerms, Vocabulary
from avicenna.utils.utils import DiscoveredDataset

COLLECTION = "als_20251206"
FOLDER = "FRS005_10"
TAG = f"{COLLECTION}_{FOLDER}_1"

GOOD_STATS = {
    "low_res_limit":  ["49.86", "49.86", "1.88"],
    "high_res_limit": ["1.85", "8.27", "1.85"],
    "completeness":   ["99.8", "100.0", "98.2"],
    "multiplicity":   ["12.4", "13.1", "9.6"],
    "cc_half":        ["0.998", "0.999", "0.451"],
    "rpim":           ["0.028", "0.014", "0.401"],
    "i_sig_i":        ["14.2", "44.6", "1.1"],
    "unit_cell":      ["78.90", "78.90", "37.10", "90.00", "90.00", "90.00"],
    "space_group":    "P43212",
    "mosaicity":      "0.12",
}


# --------------------------------------------------------------------------- #
# Fixtures: a project record and the per-process records under it              #
# --------------------------------------------------------------------------- #

def discovered(folder, collection=COLLECTION, sweep=1):
    return DiscoveredDataset(key=f"{collection}_{folder}_{sweep}",
                             folder_name=folder,
                             data_path=f"/data/{collection}_transfer/{folder}",
                             image_prefix=f"{folder}_{sweep}")


@pytest.fixture
def project(tmp_path):
    """A project directory with info.json and an empty processing tree."""
    (tmp_path / "processing").mkdir()
    info = ProjectInfo.create_from(tmp_path, [discovered(FOLDER)])
    info.write()
    return tmp_path


def write_record(project_dir, dataset_key, form="1_P43212_refined",
                 index="index_1_900", outcome=OUTCOME_COMPLETED, steps=(),
                 stats=None, crystal_code=None, **fields):
    """One worker's record, in its own pipeline subdirectory."""
    dataset_dir = project_dir / "processing" / dataset_key
    subdir = dataset_dir / form / index
    subdir.mkdir(parents=True, exist_ok=True)
    record = ProcessInfo.start(
        subdir, dataset_key,
        proc_path=str(dataset_dir), result_path=str(dataset_dir / form),
        pipeline_subdir=str(subdir), image_dir=f"/collection/{dataset_key}",
        image_prefix="C6", image_range="1-900", data_format="cbf",
        forced=False, known_sg="P43212", ncpu=16, xds_jobs=4,
        job_id="7654321", task_id="3", **fields)
    for step_id, program in steps:
        record.note_step(step_id, program=program, status="completed",
                         message="ok", seconds=12.0)
    if stats:
        record.update(dataset_key, **stats)
    if outcome == OUTCOME_FAILED:
        record.set_outcome(outcome, failed_step="4_xds",
                           reason="integration produced no reflections")
    elif outcome != OUTCOME_RUNNING:
        record.set_outcome(outcome)
    record.write()
    return record


def gathered(project_dir):
    """Run the gather and return (loaded project record, report)."""
    info = ProjectInfo.load(project_dir)
    report = ProcessingGather(project_dir / "processing").run(info)
    info.write()
    return info, report


# --------------------------------------------------------------------------- #
# The vocabulary                                                               #
# --------------------------------------------------------------------------- #

def test_two_terms_cannot_claim_one_synonym():
    """Otherwise translation picks one of them and says nothing."""
    with pytest.raises(ValueError):
        class Ambiguous(Vocabulary):
            TERMS = {"resolution": ("High resolution limit",),
                     "cutoff":     ("High resolution limit",)}


def test_a_synonym_cannot_also_be_an_accepted_term():
    with pytest.raises(ValueError):
        class Circular(Vocabulary):
            TERMS = {"completeness": ("Completeness",),
                     "Completeness": ("comp",)}


def test_a_derived_field_is_never_translated():
    with pytest.raises(ValueError):
        class Trap(Vocabulary):
            TERMS = {"crystal_code": ("dataset_code",)}
            DERIVED = {"dataset_code": "means something else on each side"}


def test_unknown_keys_pass_through_untouched():
    """A record carries much more than the vocabulary names, and none of it
    should be lost to a table that has not caught up."""
    record = {"Completeness": ["99.8"], "job_id": "7654321"}
    assert ProcessingTerms.inbound(record) == {"completeness": ["99.8"],
                                               "job_id": "7654321"}
    assert ProcessingTerms.outbound({"completeness": ["99.8"], "job_id": "1"}) \
        == {"Completeness": ["99.8"], "job_id": "1"}


# --------------------------------------------------------------------------- #
# The transfer                                                                 #
# --------------------------------------------------------------------------- #

def test_what_the_worker_recorded_arrives_in_the_project_record(project):
    write_record(project, TAG, stats=GOOD_STATS,
                 steps=[("1_xds", "xds"), ("3_aimless", "aimless")])

    info, report = gathered(project)
    record = info[TAG]

    assert record["status"] == STATUS_PROCESSED
    assert record["failure_reason"] is None
    assert record["outcome"] == OUTCOME_COMPLETED
    for field, value in GOOD_STATS.items():
        assert record[field] == value
    assert record["image_dir"] == f"/collection/{TAG}"
    assert record["image_range"] == "1-900"
    assert record["known_sg"] == "P43212"
    assert record["ncpu"] == 16
    assert record["job_id"] == "7654321"
    assert record["pipeline_subdir"].endswith("index_1_900")
    assert [s["id"] for s in record["steps"]] == ["1_xds", "3_aimless"]
    assert report.processed == 1


def test_the_merged_data_file_reaches_the_project_record(project):
    """Refinement reads its input path from here, so it must survive the
    gather verbatim."""
    final_mtz = f"/project/processing/{TAG}/1_P43212_refined/index_1_900/" \
                "10_rfree/XDS_ASCII_ctruncate.mtz"
    write_record(project, TAG, stats=GOOD_STATS, final_mtz=final_mtz)

    info, _ = gathered(project)

    assert info[TAG]["final_mtz"] == final_mtz


def test_the_crystal_identity_is_not_overwritten(project):
    """The project record owns it; a stage that thinks it knows better is a
    stage with a bug."""
    write_record(project, TAG, stats=GOOD_STATS)

    info, _ = gathered(project)
    assert info[TAG]["crystal_code"] == FOLDER
    assert info[TAG]["dataset_code"] == TAG


def test_the_record_is_written_where_the_run_can_be_read_back(project):
    write_record(project, TAG, stats=GOOD_STATS)
    gathered(project)

    on_disk = json.loads((project / "info.json").read_text())
    assert on_disk["datasets"][TAG]["status"] == STATUS_PROCESSED


def test_several_attempts_give_one_dataset_entry(project):
    """One dataset runs as several processes -- crystal form, index range, and
    any forced-integration retry."""
    write_record(project, TAG, form="1_P43212_refined", stats=GOOD_STATS)
    worse = dict(GOOD_STATS, rpim=["0.120", "0.014", "0.401"])
    write_record(project, TAG, form="2_P41212_refined", stats=worse)

    info, _ = gathered(project)
    assert info[TAG]["attempts"] == 2
    assert info[TAG]["rpim"][0] == "0.028"          # the better one


# --------------------------------------------------------------------------- #
# One crystal, two collections                                                 #
# --------------------------------------------------------------------------- #

def test_two_collections_of_one_crystal_are_compared(tmp_path):
    """The crystal's identity is the crystal code, so two collections of one
    pin compete even though their tags differ."""
    (tmp_path / "processing").mkdir()
    first, second = discovered(FOLDER, "als_20251206"), discovered(FOLDER, "als_20251209")
    ProjectInfo.create_from(tmp_path, [first, second]).write()

    write_record(tmp_path, first.key, stats=GOOD_STATS)
    write_record(tmp_path, second.key,
                 stats=dict(GOOD_STATS, rpim=["0.150", "0.014", "0.401"]))

    info, _ = gathered(tmp_path)
    assert info.status_of(first.key) == STATUS_PROCESSED
    assert info.status_of(second.key) == STATUS_FAILED
    assert info[second.key]["failure_reason"] == f"superseded by {first.key}"


def test_the_loser_of_a_crystal_names_the_winner(tmp_path):
    """A superseded dataset is sound data that lost a comparison, so the
    record says which dataset to go and look at instead."""
    (tmp_path / "processing").mkdir()
    best, worse, worst = (discovered(FOLDER, "als_20251206"),
                          discovered(FOLDER, "als_20251209"),
                          discovered(FOLDER, "als_20251210"))
    ProjectInfo.create_from(tmp_path, [best, worse, worst]).write()

    write_record(tmp_path, best.key, stats=GOOD_STATS)
    write_record(tmp_path, worse.key,
                 stats=dict(GOOD_STATS, rpim=["0.150", "0.014", "0.401"]))
    write_record(tmp_path, worst.key,
                 stats=dict(GOOD_STATS, rpim=["0.180", "0.014", "0.401"]))

    info, _ = gathered(tmp_path)
    assert info.status_of(best.key) == STATUS_PROCESSED
    for loser in (worse, worst):
        assert info[loser.key]["failure_reason"] == f"superseded by {best.key}"


def test_the_winner_is_named_by_its_tag_not_by_its_folder(tmp_path):
    """The label is the dataset tag the candidate carries, not the name of the
    folder the record was read from -- the two coincide today, and a name that
    identifies the dataset is the one worth writing into the record."""
    (tmp_path / "processing").mkdir()
    one, two = discovered(FOLDER, "als_20251206"), discovered(FOLDER, "als_20251209")
    ProjectInfo.create_from(tmp_path, [one, two]).write()
    write_record(tmp_path, one.key, stats=GOOD_STATS)
    write_record(tmp_path, two.key,
                 stats=dict(GOOD_STATS, rpim=["0.150", "0.014", "0.401"]))

    info = ProjectInfo.load(tmp_path)
    records, _ = collect(tmp_path / "processing")
    for n, attempts in enumerate(records.values()):    # folders that are not tags
        for fields in attempts:
            fields["proc_path"] = f"/old/processing/folder_{n}"

    codes = {k: r.get("crystal_code") for k, r in info.datasets.items()}
    decisions = resolve(list(info.datasets), records,
                        select(records, codes), [], RunSurvey(tmp_path / "processing"))
    reasons = {d.key: d.reason for d in decisions}
    assert reasons[two.key] == f"superseded by {one.key}"


def test_a_dataset_that_lost_on_quality_is_not_called_superseded(tmp_path):
    """The two ways out of the filter read differently: one names a winner,
    the other says the data did not measure up."""
    (tmp_path / "processing").mkdir()
    one = discovered(FOLDER, "als_20251206")
    ProjectInfo.create_from(tmp_path, [one]).write()
    write_record(tmp_path, one.key,
                 stats=dict(GOOD_STATS, cc_half=["0.998", "0.999", "0.021"]))

    info, _ = gathered(tmp_path)
    assert info.status_of(one.key) == STATUS_FAILED
    assert "superseded" not in info[one.key]["failure_reason"]


def test_the_comparison_survives_a_moved_run_directory(tmp_path):
    """Grouping on the crystal code rather than on a path is the point."""
    (tmp_path / "processing").mkdir()
    one, two = discovered(FOLDER, "als_20251206"), discovered(FOLDER, "als_20251209")
    ProjectInfo.create_from(tmp_path, [one, two]).write()
    write_record(tmp_path, one.key, stats=GOOD_STATS)
    write_record(tmp_path, two.key, stats=GOOD_STATS)

    info = ProjectInfo.load(tmp_path)
    records, _ = collect(tmp_path / "processing")
    for attempts in records.values():                 # as if collected elsewhere
        for fields in attempts:
            fields["proc_path"] = fields["proc_path"].replace(str(tmp_path), "/old")
    codes = {k: r.get("crystal_code") for k, r in info.datasets.items()}

    selection = select(records, codes)
    assert len(selection.winners) == 1


# --------------------------------------------------------------------------- #
# The states a dataset can be left in                                          #
# --------------------------------------------------------------------------- #

def test_a_dataset_rejected_before_submission_says_so(project):
    rejected = project / "processing" / ".rejected"
    rejected.mkdir()
    (rejected / f"{TAG}.txt").write_text("partial dataset (40 images, minimum is 100)\n")

    info, _ = gathered(project)
    assert info[TAG]["outcome"] == OUTCOME_REJECTED
    assert info.status_of(TAG) == STATUS_FAILED
    assert "partial dataset" in info[TAG]["failure_reason"]


def test_a_job_that_died_before_its_first_step_is_still_accounted_for(project):
    (project / "processing" / TAG / "1_P43212_refined" / "index_1_900").mkdir(parents=True)

    info, _ = gathered(project)
    assert info[TAG]["outcome"] == OUTCOME_UNPROCESSED
    assert "no record" in info[TAG]["failure_reason"]


def test_a_record_still_running_names_the_step_it_died_on(project):
    write_record(project, TAG, outcome=OUTCOME_RUNNING,
                 steps=[("1_xds", "xds"), ("2_pointless", "pointless")])

    info, _ = gathered(project)
    assert info[TAG]["outcome"] == OUTCOME_RUNNING
    assert "2_pointless" in info[TAG]["failure_reason"]


def test_a_failed_job_reports_its_step_and_its_reason(project):
    write_record(project, TAG, outcome=OUTCOME_FAILED, steps=[("1_xds", "xds")])

    info, _ = gathered(project)
    assert info[TAG]["failed_step"] == "4_xds"
    assert info[TAG]["failure_reason"].startswith("4_xds: ")


def test_a_dataset_that_scaled_badly_is_not_passed_on(project):
    write_record(project, TAG,
                 stats=dict(GOOD_STATS, cc_half=["0.998", "0.999", "0.021"]))

    info, _ = gathered(project)
    assert info.status_of(TAG) == STATUS_FAILED
    assert "quality thresholds" in info[TAG]["failure_reason"]
    assert info[TAG]["cc_half"][-1] == "0.021"      # and the numbers are kept


def test_a_dataset_that_never_ran_at_all_is_still_in_the_account(project):
    info, report = gathered(project)

    assert info.status_of(TAG) == STATUS_FAILED
    assert info[TAG]["failure_reason"] == "no job ran for this dataset"
    assert report.failed == 1


def test_a_processing_failure_names_its_stage(project):
    write_record(project, TAG, outcome=OUTCOME_FAILED, steps=[("1_xds", "xds")])

    info, _ = gathered(project)
    assert info[TAG][FAILED_STAGE] == STAGE_PROCESS


def test_a_dataset_rejected_before_submission_also_names_the_stage(project):
    rejected = project / "processing" / ".rejected"
    rejected.mkdir()
    (rejected / f"{TAG}.txt").write_text("partial dataset\n")

    info, _ = gathered(project)
    assert info[TAG][FAILED_STAGE] == STAGE_PROCESS


def test_a_processed_dataset_carries_no_failed_stage(project):
    write_record(project, TAG, stats=GOOD_STATS)
    info = ProjectInfo.load(project)
    info[TAG][FAILED_STAGE] = STAGE_REFINE          # left by an earlier run
    info.write()

    info, _ = gathered(project)
    assert info.status_of(TAG) == STATUS_PROCESSED
    assert FAILED_STAGE not in info[TAG]


def test_a_processing_failure_is_not_refinement_input(project):
    write_record(project, TAG, outcome=OUTCOME_FAILED, steps=[("1_xds", "xds")])
    info = ProjectInfo.load(project)
    info[TAG][FAILED_STAGE] = STAGE_REFINE          # left by an earlier run
    info.write()

    info, _ = gathered(project)
    assert TAG not in info.select_for_refinement()


# --------------------------------------------------------------------------- #
# A damaged record                                                             #
# --------------------------------------------------------------------------- #

def test_a_damaged_record_costs_its_dataset_and_not_the_run(project):
    """Writes are atomic, so an unreadable file is damage after a complete
    write -- not something the process did, and not grounds for failing every
    other dataset in the run."""
    other = discovered("FRS005_11")
    info = ProjectInfo.load(project)
    info.update(other.key, crystal_code=other.folder_name, status="imported")
    info.write()

    write_record(project, other.key, stats=GOOD_STATS)
    subdir = project / "processing" / TAG / "1_P43212_refined" / "index_1_900"
    subdir.mkdir(parents=True)
    (subdir / f"{TAG}_info.json").write_text("{ this is not json")

    info, report = gathered(project)

    assert info.status_of(other.key) == STATUS_PROCESSED
    assert info[TAG]["outcome"] == OUTCOME_UNPROCESSED
    assert "preserved as" in info[TAG]["failure_reason"]
    assert len(report.damaged) == 1
    assert "unreadable record" in report.summary()


def test_the_preserved_copy_is_named_in_the_record(project):
    subdir = project / "processing" / TAG / "1_P43212_refined" / "index_1_900"
    subdir.mkdir(parents=True)
    (subdir / f"{TAG}_info.json").write_text("{ this is not json")

    info, _ = gathered(project)
    preserved = [p.name for p in subdir.iterdir() if ".bad." in p.name]

    assert len(preserved) == 1
    assert preserved[0] in info[TAG]["failure_reason"]


# --------------------------------------------------------------------------- #
# The pieces on their own                                                      #
# --------------------------------------------------------------------------- #

def test_selection_is_a_function_of_the_records_alone(project):
    """Gather and select are kept separate from the cluster so they can be
    exercised without one."""
    records = {TAG: [dict(GOOD_STATS, outcome=OUTCOME_COMPLETED,
                          proc_path="/proc/" + TAG)]}
    selection = select(records, {TAG: FOLDER})

    assert list(selection.winners) == [TAG]
    assert selection.engine.superseded == []


def test_a_gather_pattern_cannot_pick_up_the_project_record(project):
    write_record(project, TAG, stats=GOOD_STATS)
    records, damaged = collect(project)          # from the project dir itself

    assert list(records) == [TAG]
    assert damaged == []


def test_an_absent_rejection_directory_is_not_an_error(project):
    survey = RunSurvey(project / "processing")
    assert survey.rejections == {}
    assert not survey.started(TAG)


def test_every_dataset_gets_a_decision(project):
    """In the project record's own order, whatever each one did."""
    info = ProjectInfo.load(project)
    decisions = resolve(list(info.datasets), {},
                        select({}, {}), [], RunSurvey(project / "processing"))

    assert [d.key for d in decisions] == list(info.datasets)


# --------------------------------------------------------------------------- #
# The duplicate-collection report                                              #
# --------------------------------------------------------------------------- #

def _two_collections(tmp_path):
    (tmp_path / "processing").mkdir()
    one, two = discovered(FOLDER, "als_20251206"), discovered(FOLDER, "als_20251209")
    ProjectInfo.create_from(tmp_path, [one, two]).write()
    write_record(tmp_path, one.key, stats=GOOD_STATS)
    write_record(tmp_path, two.key,
                 stats=dict(GOOD_STATS, rpim=["0.150", "0.014", "0.401"]))
    return one, two


def test_the_report_marks_which_collection_of_a_crystal_was_kept(tmp_path):
    from avicenna.pipelines.gather import duplicate_report
    one, two = _two_collections(tmp_path)
    info, report = gathered(tmp_path)

    lines = duplicate_report(info, report.decisions)
    kept = next(l for l in lines if one.key in l)
    lost = next(l for l in lines if two.key in l)

    assert lines[0].startswith("Duplicate collections: 1 crystal(s)")
    assert FOLDER in lines
    assert kept.lstrip().startswith("*") and kept.endswith("selected")
    assert not lost.lstrip().startswith("*")
    assert "superseded" not in lost and lost.endswith("0.998")
    assert "0.150" in lost
    assert lines.index(kept) < lines.index(lost)     # earlier collection first


def test_no_duplicates_means_no_report(project):
    from avicenna.pipelines.gather import duplicate_report
    write_record(project, TAG, stats=GOOD_STATS)
    info, report = gathered(project)

    assert duplicate_report(info, report.decisions) == []
