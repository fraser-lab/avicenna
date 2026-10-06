"""Tests for which datasets analysis takes as input.

The analysed project's info.json is the input, as it is for refinement: every
refined dataset whose model is on disk, including one an earlier analysis
failed.  The cases worth pinning are the datasets left out (refinement failed,
model missing, not recorded at all) and the one let back in (analysis failed).
"""

import pytest

from avicenna.utils.info import (FAILED_STAGE, STAGE_ANALYSIS, STAGE_REFINE,
                                 STATUS_ANALYZED, STATUS_PROCESSED,
                                 STATUS_REFINED, ProjectInfo)
from avicenna.utils.phil.analysis.pandda_preparer import PanDDAPreparer
from avicenna.utils.utils import DiscoveredDataset

REFINED = "als_20251206_FRS005_10_1"
ANALYSED = "als_20251206_FRS006_04_1"
ANALYSIS_FAILED = "als_20251206_FRS007_02_1"
REFINE_FAILED = "als_20251206_FRS008_01_1"
PROCESSED_ONLY = "als_20251206_FRS009_03_1"
MODEL_GONE = "als_20251206_FRS010_05_1"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    tags = [REFINED, ANALYSED, ANALYSIS_FAILED, REFINE_FAILED, PROCESSED_ONLY,
            MODEL_GONE]
    info = ProjectInfo.create_from(root, [
        DiscoveredDataset(key=tag, folder_name=tag, data_path=f"/data/{tag}",
                          image_prefix="C6") for tag in tags])
    for tag in tags:
        job = root / "qrefine" / tag
        job.mkdir(parents=True)
        (job / "final_refined.pdb").write_text("ATOM\n")
        (job / "final_refined.mtz").write_text("x\n")
        info.update(tag, final_refined_pdb=str(job / "final_refined.pdb"),
                    final_refined_mtz=str(job / "final_refined.mtz"),
                    refine_job_dir=str(job))
    for tag in (REFINED, MODEL_GONE):
        info.set_status(tag, STATUS_REFINED)
    info.set_status(ANALYSED, STATUS_ANALYZED)
    info.fail(ANALYSIS_FAILED, "pandda.analyse did not finish")
    info[ANALYSIS_FAILED][FAILED_STAGE] = STAGE_ANALYSIS
    info.fail(REFINE_FAILED, "2_refmac: refmac stopped")
    info[REFINE_FAILED][FAILED_STAGE] = STAGE_REFINE
    info.set_status(PROCESSED_ONLY, STATUS_PROCESSED)
    info.write()
    (root / "qrefine" / MODEL_GONE / "final_refined.mtz").unlink()
    return root


def recorded(project):
    jobs = PanDDAPreparer([project], project_dir=project)._recorded_jobs(project)
    return [job_dir.name for job_dir, _pdb, _mtz in jobs]


def test_refined_and_analysed_datasets_are_input(project):
    assert REFINED in recorded(project)
    assert ANALYSED in recorded(project)


def test_a_dataset_an_earlier_analysis_failed_is_input_again(project):
    assert ANALYSIS_FAILED in recorded(project)


def test_a_dataset_refinement_did_not_finish_is_not_input(project):
    jobs = recorded(project)
    assert REFINE_FAILED not in jobs
    assert PROCESSED_ONLY not in jobs


def test_a_recorded_model_missing_on_disk_is_named(project, capsys):
    assert MODEL_GONE not in recorded(project)
    assert f"{MODEL_GONE} not analysed" in capsys.readouterr().out


def test_a_refinement_folder_the_record_does_not_name_is_not_input(project):
    stray = project / "qrefine" / "hand_made_job"
    stray.mkdir()
    (stray / "final_refined.pdb").write_text("ATOM\n")
    (stray / "final_refined.mtz").write_text("x\n")
    assert "hand_made_job" not in recorded(project)


def test_the_input_is_in_dataset_tag_order(project):
    assert recorded(project) == sorted(recorded(project))


def test_no_record_means_no_input(tmp_path, capsys):
    assert PanDDAPreparer([tmp_path], project_dir=tmp_path)._recorded_jobs(tmp_path) == []
    assert "no info.json" in capsys.readouterr().out
