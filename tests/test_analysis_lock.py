"""Tests for the analysis run lock's hand-off to the cluster.

The CLI records the pandda job id and its completion sentinel's id in the lock
as soon as qsub returns them, and the sentinel -- not the pandda job --
releases the lock when it has recorded the results.  The cases worth pinning
are that both ids reach the lock before the CLI starts following the run, that
a failed submission hands nothing over, and that the pandda script itself no
longer touches the lock.
"""

import pytest

from avicenna.pipelines.sentinel import AnalysisSentinel
from avicenna.utils.info import STATUS_REFINED, ProjectInfo
from avicenna.utils.utils import DiscoveredDataset
from avicenna.utils.phil.analysis import pandda_preparer
from avicenna.utils.phil.analysis.pandda_preparer import PanDDAPreparer

PANDDA_JOB = "5061234"
SENTINEL_JOB = "5061299"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory with two finished refinement jobs; pandda writes here."""
    tags = ("als_20251206_FRS006_04_1", "als_20251206_FRS005_10_1")
    info = ProjectInfo.create_from(tmp_path, [
        DiscoveredDataset(key=tag, folder_name=tag, data_path=f"/data/{tag}",
                          image_prefix="C6") for tag in tags])
    for tag in tags:
        job = tmp_path / "qrefine" / tag
        job.mkdir(parents=True)
        (job / "final_refined.pdb").write_text("ATOM\n")
        (job / "final_refined.mtz").write_text("x\n")
        info.update(tag, final_refined_pdb=str(job / "final_refined.pdb"),
                    final_refined_mtz=str(job / "final_refined.mtz"),
                    refine_job_dir=str(job))
        info.set_status(tag, STATUS_REFINED)
    info.write()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def events(monkeypatch):
    """Record the order of submission, hand-off and following."""
    seen = []

    def submit(self, hold_job_id, logs_dir=None):
        seen.append(("held on", hold_job_id, self.project_dir, self.source))
        return SENTINEL_JOB

    monkeypatch.setattr(AnalysisSentinel, "submit", submit)
    monkeypatch.setattr(AnalysisSentinel, "follow",
                        lambda self, start=0, log_time_out=0: seen.append("follow"))
    return seen


def preparer(project, on_submit, **kw):
    return PanDDAPreparer([project], cpus=4, on_submit=on_submit, **kw)


def test_both_job_ids_reach_the_lock_before_following(project, events, monkeypatch):
    monkeypatch.setattr(pandda_preparer, "qsub_terse", lambda args: PANDDA_JOB)

    preparer(project, lambda *ids: events.append(("submitted", *ids))).run()

    assert events == [("held on", PANDDA_JOB, project, project),
                      ("submitted", PANDDA_JOB, SENTINEL_JOB), "follow"]


def test_a_failed_submission_hands_nothing_over(project, events, monkeypatch, capsys):
    monkeypatch.setattr(pandda_preparer, "qsub_terse", lambda args: None)

    preparer(project, lambda *ids: events.append(("submitted", *ids))).run()

    assert events == []
    assert "was not submitted" in capsys.readouterr().out


def test_without_a_sentinel_there_is_nothing_to_follow(project, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(pandda_preparer, "qsub_terse", lambda args: PANDDA_JOB)
    monkeypatch.setattr(AnalysisSentinel, "submit", lambda self, *a, **k: None)
    monkeypatch.setattr(AnalysisSentinel, "follow",
                        lambda self, **k: seen.append("follow"))

    preparer(project, lambda *ids: seen.append(ids)).run()

    assert seen == [(PANDDA_JOB, None)]
    assert "sentinel was not submitted" in capsys.readouterr().out


def test_the_pandda_script_leaves_the_lock_to_the_sentinel(project, events, monkeypatch):
    monkeypatch.setattr(pandda_preparer, "qsub_terse", lambda args: PANDDA_JOB)
    preparer(project, None).run()

    script = (project / "pandda" / "pandda.sh").read_text()
    assert "RunLock" not in script


def test_a_named_project_folder_holds_the_output_logs_and_lock(project, events,
                                                               monkeypatch, tmp_path):
    """Nothing lands in the directory the command was run from."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(pandda_preparer, "qsub_terse", lambda args: PANDDA_JOB)

    preparer(project, None, project_dir=project).run()

    assert (project / "pandda" / "pandda.sh").is_file()
    assert (project / "pandda_logs").is_dir()
    assert events[0][2] == project
    assert list(elsewhere.iterdir()) == []


def test_the_project_folder_comes_from_the_output_directory(tmp_path, monkeypatch):
    from avicenna.command_line.vic_analysis import _project_dir
    from avicenna.utils.phil import AnalysisPHIL
    monkeypatch.chdir(tmp_path)

    phil = AnalysisPHIL()
    assert _project_dir(phil) == tmp_path.resolve()

    phil.adopt_phil_string('analysis { output { directory = "run2" } }')
    assert _project_dir(phil) == (tmp_path / "run2").resolve()
