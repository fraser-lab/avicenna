"""Tests for what vic.analysis does with a folder an earlier run already used.

An analysis is complete once pandda.analyse has written its completion marker;
anything else in its output folder is an incomplete run.  The cases worth
pinning are the refusals (which must write nothing), resume going ahead
without touching the folder, and --overwrite removing only this stage's results
and logs.
"""

import pytest

from avicenna.command_line.vic_analysis import _existing_results
from avicenna.utils.utils import EXIT_RUN_COMPLETE, EXIT_RUN_INCOMPLETE


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


def start(project):
    """An analysis that was interrupted: output, but no completion marker."""
    data = project / "pandda" / "data" / "VIC_x0001"
    data.mkdir(parents=True)
    (data / "final_refined.pdb").write_text("x\n")
    return project / "pandda"


def finish(project):
    pandda = start(project)
    (pandda / "pandda.done").write_text("")
    return pandda


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


def test_an_empty_folder_goes_ahead(project):
    assert _existing_results(project) is None


def test_an_empty_pandda_folder_is_not_a_run(project):
    (project / "pandda").mkdir()
    assert _existing_results(project) is None


def test_an_incomplete_run_is_refused_without_a_flag(project, capsys):
    start(project)
    assert _existing_results(project) == EXIT_RUN_INCOMPLETE
    assert "incomplete analysis" in capsys.readouterr().out


def test_a_complete_run_is_refused_without_a_flag(project, capsys):
    finish(project)
    assert _existing_results(project) == EXIT_RUN_COMPLETE
    assert "Results found for all data" in capsys.readouterr().out


def test_a_refusal_leaves_the_folder_as_it_was(project):
    start(project)
    (project / "analysis_pipeline.log").write_text("earlier run\n")
    before = snapshot(project)

    assert _existing_results(project) == EXIT_RUN_INCOMPLETE
    assert snapshot(project) == before


def test_resume_reruns_an_incomplete_run_in_place(project):
    start(project)
    before = snapshot(project)

    assert _existing_results(project, resume=True) is None
    assert snapshot(project) == before


def test_resume_on_a_complete_run_has_nothing_to_do(project, capsys):
    finish(project)
    before = snapshot(project)

    assert _existing_results(project, resume=True) == 0
    assert "nothing to resume" in capsys.readouterr().out
    assert snapshot(project) == before


def test_resume_on_an_empty_folder_simply_runs(project):
    """A wrapper can pass --resume every time."""
    assert _existing_results(project, resume=True) is None


def test_overwrite_clears_this_stages_results_and_logs(project):
    finish(project)
    (project / "pandda_logs").mkdir()
    (project / "analysis_pipeline.log").write_text("x\n")

    assert _existing_results(project, overwrite=True) is None

    left = sorted(p.name for p in project.iterdir())
    assert left == ["analysis_pipeline.log.1"]


def test_overwrite_keeps_settings_and_other_stages(project):
    finish(project)
    keep = ["analysis.phil", "analysis_default.phil", "refine.phil",
            "processing.phil", "qrefine.log", "processing.log", "info.json",
            "qrefine_list.txt", "avicenna_run.lock"]
    for name in keep:
        (project / name).write_text("x\n")
    for name in ("processing", "qrefine"):
        (project / name).mkdir()

    _existing_results(project, overwrite=True)

    for name in keep + ["processing", "qrefine"]:
        assert (project / name).exists(), name


def test_each_overwrite_keeps_its_own_numbered_log(project):
    for run in ("first", "second"):
        (project / "analysis_pipeline.log").write_text(f"{run} run\n")
        _existing_results(project, overwrite=True)

    assert (project / "analysis_pipeline.log.1").read_text() == "first run\n"
    assert (project / "analysis_pipeline.log.2").read_text() == "second run\n"
    assert not (project / "analysis_pipeline.log").exists()


def test_resume_and_overwrite_together_are_a_usage_error():
    from avicenna.command_line.vic_analysis import parser
    with pytest.raises(SystemExit) as stop:
        parser.parse_args(["--resume", "--overwrite"])
    assert stop.value.code == 2
