"""Tests for the info.json initialization at the head of vic.process.

This is the step that fixes dataset identity for the whole run, so the cases
worth pinning are the ones where it must NOT stop the run (no crystal list, an
unwritable record) and the one where it must (a crystal list the user gave that
cannot be read).  Everything prints to stdout, which is what puts the report in
processing.log when the caller redirects.
"""

import json

import pytest

from avicenna.command_line.vic_process import _existing_results, _init_info_json
from avicenna.utils.info import FILENAME, PREV_SUFFIX
from avicenna.utils.utils import EXIT_RUN_COMPLETE, EXIT_RUN_INCOMPLETE

from tests.test_survey import job_dir, record as job_record, reject

CRYSTAL_LIST = """\
DestinationName,DestinationLocation,Library,Well,SMILES,Compound ID
FRS006,4,EE_1,A1,SMILES-A1,ZA1
FRS005,10,EE_1,B2,SMILES-B2,ZB2
FRS005,99,EE_1,C3,SMILES-C3,ZC3
"""


@pytest.fixture
def collection(tmp_path):
    """A transfer directory holding three pins, one of them frameless."""
    root = tmp_path / "data" / "als_20251206_transfer"
    for pin, prefix in (("FRS006_04", "FRS006_04_1"), ("FRS005_10", "FRS005_10_1")):
        folder = root / pin
        folder.mkdir(parents=True)
        for number in (1, 2, 3):
            (folder / f"{prefix}_{number:05d}.cbf").touch()
    (root / "FRS005_11").mkdir()
    return root


@pytest.fixture
def pins(collection):
    return [f"{collection},{pin}"
            for pin in ("FRS006_04", "FRS005_10", "FRS005_11")]


@pytest.fixture
def crystal_list(tmp_path):
    path = tmp_path / "crystals.csv"
    path.write_text(CRYSTAL_LIST)
    return str(path)


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


def record(project):
    return json.loads((project / FILENAME).read_text())


# --------------------------------------------------------------------------- #
# With a crystal list                                                          #
# --------------------------------------------------------------------------- #

def test_datasets_are_keyed_by_tag_and_carry_their_fragment(pins, crystal_list,
                                                            project):
    _init_info_json(pins, crystal_list, project)
    datasets = record(project)["datasets"]

    assert list(datasets) == ["als_20251206_FRS006_04_1", "als_20251206_FRS005_10_1"]
    matched = datasets["als_20251206_FRS006_04_1"]
    assert matched["crystal_code"] == "FRS006_4"
    assert matched["SMILES"] == "SMILES-A1"
    assert matched["image_prefix"] == "FRS006_04_1"
    assert matched["data_path"].endswith("/FRS006_04")


def test_both_directions_of_the_mismatch_are_reported(pins, crystal_list,
                                                      project, capsys):
    _init_info_json(pins, crystal_list, project)
    printed = capsys.readouterr().out
    assert "DATA NOT FOUND  -- FRS005_99" in printed
    assert "Crystal list: 2/2 pins matched" in printed

    meta = record(project)["meta"]
    assert meta["crystal_list"] == crystal_list
    assert meta["unmatched"]["entries"] == ["FRS005_99"]


def test_a_frameless_folder_is_reported_but_not_recorded(pins, crystal_list,
                                                         project, capsys):
    """No frames means no sweep, so there is no tag to key it by."""
    _init_info_json(pins, crystal_list, project)
    assert "NO IMAGE DATA   -- FRS005_11 skipped" in capsys.readouterr().out
    assert not [key for key in record(project)["datasets"] if "FRS005_11" in key]


# --------------------------------------------------------------------------- #
# Without one, and with a broken one                                           #
# --------------------------------------------------------------------------- #

def test_no_crystal_list_still_writes_the_record(pins, project):
    """info.json must never be a precondition for running."""
    _init_info_json(pins, None, project)
    data = record(project)
    assert len(data["datasets"]) == 2
    assert "crystal_list" not in data["meta"]
    assert "SMILES" not in data["datasets"]["als_20251206_FRS006_04_1"]


def test_an_unreadable_crystal_list_stops_the_run_before_anything_is_written(
        pins, project, tmp_path, capsys):
    """A user-supplied file that cannot be read is worth catching here."""
    missing = str(tmp_path / "nope.xlsx")
    with pytest.raises(SystemExit) as stop:
        _init_info_json(pins, missing, project)
    assert "nope.xlsx" in str(stop.value)
    assert not (project / FILENAME).exists()


def test_a_crystal_list_without_the_needed_columns_names_what_is_missing(
        pins, project, tmp_path):
    path = tmp_path / "wrong.csv"
    path.write_text("Barcode,Puck\nFRS006,4\n")
    with pytest.raises(SystemExit) as stop:
        _init_info_json(pins, str(path), project)
    assert "DestinationName" in str(stop.value)


# --------------------------------------------------------------------------- #
# Re-runs                                                                      #
# --------------------------------------------------------------------------- #

def test_a_rerun_overwrites_and_keeps_one_generation(pins, crystal_list, project):
    """Re-running is meant to produce different results; .prev is the net."""
    _init_info_json(pins, crystal_list, project)
    first = record(project)["meta"]["created"]

    _init_info_json(pins, None, project)
    again = record(project)
    assert "crystal_list" not in again["meta"]

    previous = json.loads((project / (FILENAME + PREV_SUFFIX)).read_text())
    assert previous["meta"]["created"] == first
    assert previous["meta"]["crystal_list"] == crystal_list


# --------------------------------------------------------------------------- #
# A run into a folder that already holds results                               #
# --------------------------------------------------------------------------- #

FIRST = "als_20251206_FRS006_04_1"
SECOND = "als_20251206_FRS005_10_1"


def processing(project):
    return project / "processing"


def finish(project, key):
    job_record(job_dir(processing(project), key=key), key=key)


def fray(project, key):
    job_dir(processing(project), key=key)


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


def test_an_empty_folder_goes_ahead(pins, project):
    assert _existing_results(pins, project) is None


def test_an_incomplete_run_is_refused_without_a_flag(pins, project, capsys):
    finish(project, FIRST)
    assert _existing_results(pins, project) == EXIT_RUN_INCOMPLETE
    assert "Results found for 1/2 datasets" in capsys.readouterr().out


def test_a_complete_run_is_refused_without_a_flag(pins, project, capsys):
    finish(project, FIRST)
    finish(project, SECOND)
    assert _existing_results(pins, project) == EXIT_RUN_COMPLETE
    assert "Results found for all data" in capsys.readouterr().out


def test_a_refusal_leaves_the_folder_as_it_was(pins, project):
    finish(project, FIRST)
    fray(project, SECOND)
    (project / "processing.log").write_text("earlier run\n")
    (project / FILENAME).write_text("{}\n")
    before = snapshot(project)

    assert _existing_results(pins, project) == EXIT_RUN_INCOMPLETE
    assert snapshot(project) == before


def test_resume_removes_only_the_unfinished_datasets(pins, project, capsys):
    finish(project, FIRST)
    fray(project, SECOND)

    assert _existing_results(pins, project, resume=True) is None
    assert (processing(project) / FIRST).is_dir()
    assert not (processing(project) / SECOND).exists()
    assert f"Removed unfinished {SECOND}" in capsys.readouterr().out


def test_resume_on_a_complete_run_has_nothing_to_do(pins, project, capsys):
    finish(project, FIRST)
    reject(processing(project), SECOND)
    before = snapshot(project)

    assert _existing_results(pins, project, resume=True) == 0
    assert "nothing to resume" in capsys.readouterr().out
    assert snapshot(project) == before


def test_resume_on_an_empty_folder_simply_runs(pins, project):
    """A wrapper can pass --resume every time."""
    assert _existing_results(pins, project, resume=True) is None


def test_overwrite_clears_this_stages_results_and_logs(pins, project):
    finish(project, FIRST)
    (project / "processing_logs").mkdir()
    for name in ("processing.log", FILENAME, FILENAME + PREV_SUFFIX,
                 "info.json.bad.20260928", "qrefine_list.txt",
                 "sentinel_processing.py", "sentinel_processing_submit.sh",
                 "sentinel_processing_submit.sh.o5059200"):
        (project / name).write_text("x\n")

    assert _existing_results(pins, project, overwrite=True) is None

    left = sorted(p.name for p in project.iterdir())
    assert left == ["processing.log.1"]


def test_overwrite_keeps_settings_and_other_stages(pins, project):
    finish(project, FIRST)
    keep = ["processing.phil", "refine.phil", "analysis.phil", "pin_list.txt",
            "avicenna_run.lock", "qrefine.log"]
    for name in keep:
        (project / name).write_text("x\n")
    (project / "qrefine").mkdir()

    _existing_results(pins, project, overwrite=True)

    for name in keep + ["qrefine"]:
        assert (project / name).exists(), name


def test_each_overwrite_keeps_its_own_numbered_log(pins, project):
    for run in ("first", "second"):
        (project / "processing.log").write_text(f"{run} run\n")
        _existing_results(pins, project, overwrite=True)

    assert (project / "processing.log.1").read_text() == "first run\n"
    assert (project / "processing.log.2").read_text() == "second run\n"
    assert not (project / "processing.log").exists()


def test_resume_and_overwrite_together_are_a_usage_error():
    from avicenna.command_line.vic_process import parser
    with pytest.raises(SystemExit) as stop:
        parser.parse_args(["-p", "pin_list.txt", "--resume", "--overwrite"])
    assert stop.value.code == 2


# --- the terminal shows what the sentinel wrote -------------------------------

def test_what_another_process_appended_is_echoed_as_written(tmp_path, capsys):
    from avicenna.utils.utils import echo_log, log_offset
    log = tmp_path / "processing.log"
    log.write_text("this run's own line\n")
    start = log_offset(log)
    table = "=====\nDuplicate collections: ...\n  * tag_1  selected\n=====\n"
    with open(log, "a") as fh:
        fh.write("[ts] INFO  [ sentinel ]  info.json: 1 of 2 datasets selected\n")
        fh.write(table)

    end = echo_log(log, start)

    out = capsys.readouterr().out
    assert out == log.read_text()[len("this run's own line\n"):]
    assert end == log_offset(log)
    assert echo_log(log, end) == end
    assert capsys.readouterr().out == ""


def test_a_replaced_log_is_not_echoed_from_a_stale_offset(tmp_path, capsys):
    from avicenna.utils.utils import echo_log
    log = tmp_path / "processing.log"
    log.write_text("short\n")

    assert echo_log(log, 1000) == 1000
    assert echo_log(tmp_path / "absent.log", 0) == 0
    assert capsys.readouterr().out == ""
