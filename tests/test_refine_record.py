"""The record a refinement job keeps of itself, and its gather into info.json.

Refinement runs one job per dataset, so the gather selects nothing: what has
to hold is that each job's outcome reaches the dataset it belongs to -- found
by the tag the record carries, not by the job folder's name -- and that a
dataset refinement failed can be refined again.
"""

import json

import pytest

from avicenna.pipelines.gather import RefinementGather
from avicenna.pipelines.refinement import RefinementPipelineInterpreter
from avicenna.pipelines.refinement_runner import QRefineWorker, load_datasets
from avicenna.utils.info import (
    FAILED_STAGE, OUTCOME_COMPLETED, OUTCOME_FAILED, STAGE_REFINE,
    STATUS_FAILED, STATUS_PROCESSED, STATUS_REFINED, ProcessInfo, ProjectInfo,
)
from avicenna.utils.utils import DiscoveredDataset

TAG = "als_20251206_FRS005_10_1"
OTHER = "als_20251206_FRS006_04_1"

REMARK_3 = """\
REMARK   3   RESOLUTION RANGE HIGH (ANGSTROMS) : 1.85
REMARK   3   R VALUE            (WORKING SET) : 0.1934
REMARK   3   FREE R VALUE                     : 0.2311
"""


# --------------------------------------------------------------------------- #
# The job's own record                                                         #
# --------------------------------------------------------------------------- #

def _interpreter(job_dir, monkeypatch):
    params = {"dataset_tag": TAG, "pin": TAG, "suffix": "hoh",
              "mtz": "/project/processing/x/XDS_ASCII_ctruncate.mtz",
              "job_dir": str(job_dir), "job_id": TAG}
    interpreter = RefinementPipelineInterpreter(params)
    monkeypatch.setattr(interpreter, "_run_steps", lambda steps, ctx: False)
    return interpreter


def _record(job_dir):
    return ProcessInfo.load(ProcessInfo.path_in(job_dir, TAG))[TAG]


def test_a_finished_job_records_its_refined_model(tmp_path, monkeypatch):
    job_dir = tmp_path / "qrefine" / TAG
    job_dir.mkdir(parents=True)
    (job_dir / "final_hoh.pdb").write_text(REMARK_3)
    (job_dir / "final_hoh.mtz").write_text("")

    _interpreter(job_dir, monkeypatch)._run()
    record = _record(job_dir)

    assert record["outcome"] == OUTCOME_COMPLETED
    assert record["dataset_tag"] == TAG
    assert record["input_mtz"].endswith("XDS_ASCII_ctruncate.mtz")
    assert (record["rwork"], record["rfree"], record["refine_resolution"]) == \
        (0.1934, 0.2311, 1.85)
    assert record["final_refined_pdb"] == str(job_dir.resolve() / "final_refined.pdb")


def test_a_job_without_its_outputs_records_the_failed_finalize(tmp_path,
                                                               monkeypatch):
    job_dir = tmp_path / "qrefine" / TAG
    job_dir.mkdir(parents=True)

    _interpreter(job_dir, monkeypatch)._run()
    record = _record(job_dir)

    assert record["outcome"] == OUTCOME_FAILED
    assert record["failed_step"] == "finalize"
    assert "final_hoh.pdb" in record["failure_reason"]


def test_the_worker_hands_the_job_its_dataset_tag(tmp_path):
    class _Worker(QRefineWorker):
        def _extra_params(self, suffix):
            return {}

        def _sbgrid_block(self):
            return "_sbgrid_cmd = 'true'\n"

        def _worker_body(self, suffix):
            return ""

    (tmp_path / "qrefine_list.txt").write_text(
        f"{TAG},1_P43212,refined,1 900,integration not forced\n")
    dataset, = load_datasets(tmp_path, tmp_path / "qrefine_list.txt")
    dataset.resolve()
    dataset.job_dir.mkdir(parents=True)

    _Worker(dataset, {"mr_water": "true"}).write()
    params = json.loads((dataset.job_dir / "params_hoh.json").read_text())

    assert params["dataset_tag"] == TAG


# --------------------------------------------------------------------------- #
# The gather                                                                   #
# --------------------------------------------------------------------------- #

@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "qrefine").mkdir(parents=True)
    info = ProjectInfo.create_from(root, [
        DiscoveredDataset(key=tag, folder_name=tag, data_path=f"/data/{tag}",
                          image_prefix="C6") for tag in (TAG, OTHER)])
    for tag in (TAG, OTHER):
        info.update(tag, final_mtz=f"/p/{tag}.mtz", outcome=OUTCOME_COMPLETED,
                    job_id="111")
        info.set_status(tag, STATUS_PROCESSED)
    info.write()
    return root


def job_record(project, tag, outcome=OUTCOME_COMPLETED, folder=None, **fields):
    job_dir = project / "qrefine" / (folder or tag)
    job_dir.mkdir(parents=True, exist_ok=True)
    record = ProcessInfo.start(job_dir, tag, dataset_tag=tag,
                               job_dir=str(job_dir), job_id="222", **fields)
    if outcome == OUTCOME_FAILED:
        record.set_outcome(outcome, failed_step="04-refmac",
                           reason="refmac exited 1")
    else:
        record.set_outcome(outcome)
    record.write()


def gathered(project, keys=(TAG, OTHER)):
    info = ProjectInfo.load(project)
    gather = RefinementGather(project / "qrefine", keys)
    report = gather.run(info)
    info.write()
    return ProjectInfo.load(project), report, gather


def test_a_refined_job_reaches_its_dataset(project):
    job_record(project, TAG, rwork=0.19, rfree=0.23, refine_resolution=1.85,
               final_refined_pdb="/p/final_refined.pdb")

    info, report, _ = gathered(project, keys=[TAG])
    record = info[TAG]

    assert record["status"] == STATUS_REFINED
    assert (record["rwork"], record["rfree"]) == (0.19, 0.23)
    assert record["final_refined_pdb"] == "/p/final_refined.pdb"
    assert record["refine_outcome"] == OUTCOME_COMPLETED
    assert report.refined == 1


def test_the_processing_winners_own_fields_survive(project):
    """Refinement's outcome and job id are its own, and must not replace the
    ones processing recorded."""
    job_record(project, TAG)

    info, _, _ = gathered(project, keys=[TAG])

    assert info[TAG]["outcome"] == OUTCOME_COMPLETED
    assert info[TAG]["job_id"] == "111"
    assert info[TAG]["final_mtz"] == f"/p/{TAG}.mtz"


def test_the_dataset_comes_from_the_record_not_the_folder(project):
    job_record(project, TAG, folder="renamed_by_hand")

    info, _, _ = gathered(project, keys=[TAG])

    assert info[TAG]["status"] == STATUS_REFINED


def test_a_failed_job_says_where_it_failed(project):
    job_record(project, TAG, outcome=OUTCOME_FAILED)

    info, _, _ = gathered(project, keys=[TAG])

    assert info[TAG]["status"] == STATUS_FAILED
    assert info[TAG]["failure_reason"] == "04-refmac: refmac exited 1"
    assert info[TAG][FAILED_STAGE] == STAGE_REFINE


def test_a_job_that_left_no_record_is_told_from_one_that_never_ran(project):
    (project / "qrefine" / TAG).mkdir()

    info, _, _ = gathered(project)

    assert "died before its first step" in info[TAG]["failure_reason"]
    assert info[OTHER]["failure_reason"] == "no refinement job ran for this dataset"


def test_a_dataset_refinement_failed_can_be_refined_again(project):
    job_record(project, TAG, outcome=OUTCOME_FAILED)
    info, _, _ = gathered(project, keys=[TAG])

    assert TAG in info.select_for_refinement()
    assert TAG in {d.pin for d in load_datasets(project)}


def test_a_later_success_clears_the_earlier_failure(project):
    job_record(project, TAG, outcome=OUTCOME_FAILED)
    gathered(project, keys=[TAG])
    job_record(project, TAG)

    info, _, _ = gathered(project, keys=[TAG])

    assert info[TAG]["status"] == STATUS_REFINED
    assert FAILED_STAGE not in info[TAG]
    assert "refine_failed_step" not in info[TAG]


def test_a_dataset_processing_failed_stays_out_of_refinement(project):
    info = ProjectInfo.load(project)
    info.fail(OTHER, "superseded by somebody")
    info.write()

    assert OTHER not in ProjectInfo.load(project).select_for_refinement()


def test_a_tag_the_project_does_not_hold_is_reported_not_added(project):
    info, _, gather = gathered(project, keys=[TAG, "not_a_dataset"])

    assert gather.unknown == ["not_a_dataset"]
    assert "not_a_dataset" not in info


def test_the_sentinel_records_the_run_it_watched(project):
    from avicenna.pipelines.sentinel import RefinementSentinel
    job_record(project, TAG, rfree=0.23)

    RefinementSentinel(project)._merge_info_json()
    info = ProjectInfo.load(project)

    assert info[TAG]["status"] == STATUS_REFINED
    assert info[OTHER]["status"] == STATUS_FAILED
    assert "1 of 2 datasets refined" in (project / "qrefine.log").read_text()
