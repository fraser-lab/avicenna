"""Tests for what vic.refine does with a folder an earlier run already used.

A refinement job is done once it has left the canonical outputs the next stage
reads.  The cases worth pinning are the refusals (which must write nothing),
resume keeping finished jobs while redoing the rest, and --overwrite removing
only this stage's results and logs.
"""

import pytest

from avicenna.command_line.vic_refine import _existing_results
from avicenna.pipelines.refinement_runner import (REFINED_OUTPUTS, RunQRefine,
                                                  load_datasets)
from avicenna.pipelines.survey import (DONE, FRAYED, RUN_COMPLETE, RUN_INCOMPLETE,
                                       RUN_NONE, UNTOUCHED, survey_refine)
from avicenna.utils.info import (FAILED_STAGE, STAGE_PROCESS, STAGE_REFINE,
                                 STATUS_FAILED, STATUS_PROCESSED, STATUS_REFINED,
                                 ProjectInfo)
from avicenna.utils.utils import (EXIT_RUN_COMPLETE, EXIT_RUN_INCOMPLETE,
                                  DiscoveredDataset)

LIST = """\
FRS006_04,C1,,P 41 21 2,integration not forced
FRS005_10,C1,true,P 41 21 2,integration not forced
"""

FIRST = "FRS006_04"
SECOND = "FRS005_10"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "qrefine_list.txt").write_text(LIST)
    return root


@pytest.fixture
def dataset_list(project):
    return project / "qrefine_list.txt"


def job(project, name):
    path = project / "qrefine" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def finish(project, name):
    path = job(project, name)
    for output in REFINED_OUTPUTS:
        (path / output).write_text("x\n")
    return path


def fray(project, name):
    return job(project, name)


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


# --- survey ------------------------------------------------------------------

def test_the_survey_names_jobs_by_their_folder(dataset_list, project):
    finish(project, FIRST)
    survey = survey_refine(project, dataset_list)
    assert survey.classes == {FIRST: DONE, SECOND: UNTOUCHED}
    assert survey.state == RUN_INCOMPLETE


def test_a_job_with_only_one_output_is_frayed(dataset_list, project):
    path = job(project, FIRST)
    (path / REFINED_OUTPUTS[0]).write_text("x\n")
    finish(project, SECOND)
    survey = survey_refine(project, dataset_list)
    assert survey.classes[FIRST] == FRAYED
    assert survey.frayed == [FIRST]


def test_every_job_finished_is_a_complete_run(dataset_list, project):
    finish(project, FIRST)
    finish(project, SECOND)
    assert survey_refine(project, dataset_list).state == RUN_COMPLETE


def test_a_missing_list_is_not_a_run(project):
    assert survey_refine(project, project / "absent.txt").state == RUN_NONE


# --- refusals and resume -----------------------------------------------------

def test_an_empty_folder_goes_ahead(dataset_list, project):
    assert _existing_results(project, dataset_list) is None


def test_an_incomplete_run_is_refused_without_a_flag(dataset_list, project, capsys):
    finish(project, FIRST)
    assert _existing_results(project, dataset_list) == EXIT_RUN_INCOMPLETE
    assert "Results found for 1/2 datasets" in capsys.readouterr().out


def test_a_complete_run_is_refused_without_a_flag(dataset_list, project, capsys):
    finish(project, FIRST)
    finish(project, SECOND)
    assert _existing_results(project, dataset_list) == EXIT_RUN_COMPLETE
    assert "Results found for all data" in capsys.readouterr().out


def test_a_refusal_leaves_the_folder_as_it_was(dataset_list, project):
    finish(project, FIRST)
    fray(project, SECOND)
    (project / "qrefine.log").write_text("earlier run\n")
    before = snapshot(project)

    assert _existing_results(project, dataset_list) == EXIT_RUN_INCOMPLETE
    assert snapshot(project) == before


def test_resume_on_an_incomplete_run_goes_ahead(dataset_list, project):
    finish(project, FIRST)
    fray(project, SECOND)
    assert _existing_results(project, dataset_list, resume=True) is None


def test_resume_on_a_complete_run_has_nothing_to_do(dataset_list, project, capsys):
    finish(project, FIRST)
    finish(project, SECOND)
    before = snapshot(project)

    assert _existing_results(project, dataset_list, resume=True) == 0
    assert "nothing to resume" in capsys.readouterr().out
    assert snapshot(project) == before


def test_resume_on_an_empty_folder_simply_runs(dataset_list, project):
    """A wrapper can pass --resume every time."""
    assert _existing_results(project, dataset_list, resume=True) is None


# --- overwrite ---------------------------------------------------------------

def test_overwrite_clears_this_stages_results_and_logs(dataset_list, project):
    finish(project, FIRST)
    (project / "qrefine_logs").mkdir()
    for name in ("qrefine.log", "qrefine_qsub_list.txt", "qrefine_qsub_submit.sh",
                 "sentinel_refine.py", "sentinel_refine_submit.sh",
                 "sentinel_refine_submit.sh.o5059200"):
        (project / name).write_text("x\n")

    assert _existing_results(project, dataset_list, overwrite=True) is None

    left = sorted(p.name for p in project.iterdir())
    assert left == ["qrefine.log.1", "qrefine_list.txt"]


def test_overwrite_keeps_settings_and_other_stages(dataset_list, project):
    finish(project, FIRST)
    keep = ["processing.phil", "refine.phil", "refine_default.phil", "analysis.phil",
            "avicenna_run.lock", "processing.log"]
    for name in keep:
        (project / name).write_text("x\n")
    ProjectInfo.create_from(project, []).write()
    keep.append("info.json")
    (project / "processing").mkdir()

    _existing_results(project, dataset_list, overwrite=True)

    for name in keep + ["processing", "qrefine_list.txt"]:
        assert (project / name).exists(), name


def test_each_overwrite_keeps_its_own_numbered_log(dataset_list, project):
    for run in ("first", "second"):
        (project / "qrefine.log").write_text(f"{run} run\n")
        _existing_results(project, dataset_list, overwrite=True)

    assert (project / "qrefine.log.1").read_text() == "first run\n"
    assert (project / "qrefine.log.2").read_text() == "second run\n"
    assert not (project / "qrefine.log").exists()


def _refined_once(project):
    """info_project after a refinement run: one refined, one refine failure."""
    info = ProjectInfo.load(project)
    info.update(WINNER, rwork="0.19", rfree="0.23", refine_resolution="1.80",
                final_refined_pdb=str(project / "qrefine" / WINNER / "final_refined.pdb"),
                final_refined_mtz=str(project / "qrefine" / WINNER / "final_refined.mtz"),
                refine_job_dir=str(project / "qrefine" / WINNER),
                refine_outcome="completed")
    info.set_status(WINNER, STATUS_REFINED)
    info.update(FORCED, refine_job_dir=str(project / "qrefine" / FORCED),
                refine_outcome="failed", refine_failed_step="2_refmac")
    info.fail(FORCED, "2_refmac: refmac stopped")
    info[FORCED][FAILED_STAGE] = STAGE_REFINE
    info[LOSER][FAILED_STAGE] = STAGE_PROCESS
    info.write()


def test_overwrite_takes_the_deleted_results_out_of_info_json(info_project):
    _refined_once(info_project)
    before = ProjectInfo.load(info_project).select_for_refinement()

    _existing_results(info_project, overwrite=True)

    info = ProjectInfo.load(info_project)
    for tag in (WINNER, FORCED):
        assert info.status_of(tag) == STATUS_PROCESSED
        assert info[tag]["failure_reason"] is None
        for name in ("rwork", "rfree", "refine_resolution", "final_refined_pdb",
                     "final_refined_mtz", "refine_job_dir", "refine_outcome",
                     "refine_failed_step", FAILED_STAGE):
            assert name not in info[tag], (tag, name)
    assert info[WINNER]["final_mtz"]                 # processing's own fields stay
    assert info.select_for_refinement() == before


def test_overwrite_leaves_processing_decisions_alone(info_project):
    _refined_once(info_project)

    _existing_results(info_project, overwrite=True)

    info = ProjectInfo.load(info_project)
    assert info.status_of(LOSER) == STATUS_FAILED
    assert info[LOSER][FAILED_STAGE] == STAGE_PROCESS
    assert info.status_of(NO_MTZ) == STATUS_PROCESSED


def test_overwrite_with_nothing_refined_does_not_rewrite_info_json(info_project):
    path = info_project / "info.json"
    before = path.read_bytes()

    _existing_results(info_project, overwrite=True)

    assert path.read_bytes() == before


def test_resume_and_overwrite_together_are_a_usage_error():
    from avicenna.command_line.vic_refine import parser
    with pytest.raises(SystemExit) as stop:
        parser.parse_args(["--resume", "--overwrite"])
    assert stop.value.code == 2


# --- runner ------------------------------------------------------------------

class _Worker(object):
    def __init__(self, dataset):
        self.dataset = dataset

    def write(self):
        script = self.dataset.job_dir / "run.sh"
        script.write_text("#!/bin/bash\n")
        return [script]


class _Runner(RunQRefine):
    """The orchestration loop alone: no inputs to check, nothing submitted."""

    def _load_mr_params(self):
        return None

    def _validate_inputs(self, mr_params):
        pass

    def _dataset_params(self, dataset, mr_params):
        return {}

    def _make_worker(self, dataset, params, context_width):
        return _Worker(dataset)

    def _write_pandda_template(self, mr_params):
        pass


def _queued(project):
    return sorted(line.split("/")[-2] for line in
                  (project / "qrefine_qsub_list.txt").read_text().splitlines())


def test_resume_keeps_finished_jobs_and_redoes_frayed_ones(dataset_list, project):
    done = finish(project, FIRST)
    partial = fray(project, SECOND)
    (partial / "stale.log").write_text("x\n")

    _Runner(project, dataset_list, dry_run=True, resume=True).run()

    assert _queued(project) == [SECOND]
    assert all((done / name).is_file() for name in REFINED_OUTPUTS)
    assert not (partial / "stale.log").exists()


def test_without_resume_every_job_is_redone(dataset_list, project):
    done = finish(project, FIRST)

    _Runner(project, dataset_list, dry_run=True).run()

    assert _queued(project) == sorted([FIRST, SECOND])
    assert not (done / REFINED_OUTPUTS[0]).exists()


def test_a_list_naming_one_dataset_twice_is_refused(project):
    """Every job folder is named for its dataset, so a second row for the same
    dataset would replace the first one's job."""
    (project / "twice.txt").write_text(
        LIST + "FRS006_04,C2,,P 1 21 1,integration not forced\n")

    with pytest.raises(ValueError, match="FRS006_04"):
        load_datasets(project, project / "twice.txt")


def test_a_listed_dataset_is_refined_in_a_folder_named_for_it(dataset_list, project):
    datasets = load_datasets(project, dataset_list)
    for d in datasets:
        d.resolve()

    assert [d.job_dir for d in datasets] == [project / "qrefine" / FIRST,
                                             project / "qrefine" / SECOND]


# --- info.json as the input --------------------------------------------------

COLLECTION = "als_20251206"
WINNER = f"{COLLECTION}_FRS005_10_1"
FORCED = f"{COLLECTION}_FRS006_04_1"
LOSER = f"{COLLECTION}_FRS005_10_2"
NO_MTZ = f"{COLLECTION}_FRS007_01_1"


def _mtz(project, tag, *parts):
    path = project.joinpath("processing", tag, *parts, "10_rfree",
                            "XDS_ASCII_ctruncate.mtz")
    return str(path)


@pytest.fixture
def info_project(tmp_path):
    """A project whose info.json holds two winners (one forced), a dataset
    that lost to another collection, and a winner without its data file."""
    root = tmp_path / "project"
    root.mkdir()
    tags = [WINNER, FORCED, LOSER, NO_MTZ]
    info = ProjectInfo.create_from(root, [
        DiscoveredDataset(key=tag, folder_name=tag.split("_", 2)[2][:-2],
                          data_path=f"/data/{tag}", image_prefix="C6")
        for tag in tags])
    info.update(WINNER, result_path=str(root / "processing" / WINNER / "1_P43212_refined"),
                image_range="1-900", forced=False,
                final_mtz=_mtz(root, WINNER, "1_P43212_refined", "index_1_900"))
    info.update(FORCED, result_path=str(root / "processing" / FORCED / "1_P43212_refined"),
                image_range="1-900", forced=True, forced_sg="P43212",
                final_mtz=_mtz(root, FORCED, "1_P43212_refined", "index_1_900",
                               "1_P43212_force"))
    info.update(NO_MTZ, image_range="1-900", forced=False)
    for tag in (WINNER, FORCED, NO_MTZ):
        info.set_status(tag, STATUS_PROCESSED)
    info.set_status(LOSER, STATUS_FAILED, failure_reason=f"superseded by {WINNER}")
    info.write()
    return root


def test_the_winners_are_refined_from_their_recorded_data(info_project):
    datasets = {d.pin: d for d in load_datasets(info_project)}

    assert sorted(datasets) == sorted([WINNER, FORCED])
    winner = datasets[WINNER]
    assert winner.job_dir == info_project / "qrefine" / WINNER
    assert str(winner.mtz_path) == _mtz(info_project, WINNER, "1_P43212_refined",
                                        "index_1_900")
    assert winner.forced == "not_forced"
    assert datasets[FORCED].forced == "forced_P43212"


def test_a_winner_without_its_data_file_is_named_not_dropped(info_project, capsys):
    datasets = load_datasets(info_project, report=True)

    assert NO_MTZ not in {d.pin for d in datasets}
    assert f"WARNING: {NO_MTZ} not refined" in capsys.readouterr().out


def test_the_override_list_replaces_info_json(info_project):
    (info_project / "qrefine_list.txt").write_text(LIST)

    datasets = load_datasets(info_project, info_project / "qrefine_list.txt")

    assert [d.pin for d in datasets] == [FIRST, SECOND]


def test_the_survey_reads_the_same_datasets_the_run_would(info_project):
    finish(info_project, WINNER)

    survey = survey_refine(info_project)

    assert survey.classes == {WINNER: DONE, FORCED: UNTOUCHED}


def test_the_runner_queues_the_winners(info_project):
    _Runner(info_project, dry_run=True).run()

    assert _queued(info_project) == sorted([WINNER, FORCED])


def test_nothing_to_refine_writes_nothing(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()

    assert _Runner(root, dry_run=True).run() is None
    assert "no datasets to refine" in capsys.readouterr().out
    assert list(root.iterdir()) == []


# --- readers -----------------------------------------------------------------

def test_the_watcher_follows_the_info_json_winners(info_project):
    from avicenna.utils.utils import QRefineWatcher
    watcher = QRefineWatcher(procpath=info_project)

    assert watcher.datasets == {WINNER: {WINNER: (None, None)},
                                FORCED: {FORCED: (None, None)}}


def test_the_watcher_follows_the_override_list(info_project):
    from avicenna.utils.utils import QRefineWatcher
    (info_project / "picked.txt").write_text(LIST)
    watcher = QRefineWatcher(procpath=info_project,
                             dataset_list=info_project / "picked.txt")

    assert sorted(watcher.datasets) == sorted([FIRST, SECOND])


# --- the command line refuses before anything is written ---------------------

def _main(monkeypatch, project, *argv):
    import sys
    from avicenna.command_line import vic_refine
    monkeypatch.chdir(project)
    monkeypatch.setattr(vic_refine, "check_programs", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["vic.refine", "-o", str(project), *argv])
    with pytest.raises(SystemExit) as stop:
        vic_refine.main()
    return str(stop.value.code)


def test_nothing_to_refine_is_refused_before_the_lock(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()

    message = _main(monkeypatch, root)

    assert "no datasets to refine" in message
    assert list(root.iterdir()) == []


def test_a_list_naming_a_dataset_twice_is_refused_on_the_command_line(
        tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "twice.txt").write_text(
        LIST + "FRS006_04,C2,,P 1 21 1,integration not forced\n")

    message = _main(monkeypatch, root, "-l", str(root / "twice.txt"))

    assert message.startswith("ERROR:") and "FRS006_04" in message
    assert sorted(p.name for p in root.iterdir()) == ["twice.txt"]
