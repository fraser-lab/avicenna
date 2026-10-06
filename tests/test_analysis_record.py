"""Tests for the analysis stage's record in info.json, and the info.csv
derived from it.

pandda.analyse runs once over every dataset; its completion sentinel finds the
datasets by the panDDA code the preparer recorded in info.json and records
what panDDA made of each one.  The cases worth pinning are the three things a
dataset can be left as (analysed, not analysed, missing from the summary), a
run that did not finish, a dataset that was not in the run, the ligands that
were actually placed, info.csv, and the --overwrite resets that undo it all.
"""

import pytest

from avicenna.command_line.vic_analysis import _existing_results
from avicenna.pipelines.gather import (AnalysisGather, reset_analysis,
                                       reset_refinement)
from avicenna.pipelines.sentinel import AnalysisSentinel
from avicenna.utils.info import (FAILED_STAGE, STAGE_ANALYSIS, STAGE_REFINE,
                                 STATUS_ANALYZED, STATUS_FAILED,
                                 STATUS_PROCESSED, STATUS_REFINED, ProjectInfo)
from avicenna.utils.utils import (SENTINEL_CONTEXT, SENTINEL_DONE,
                                  SENTINEL_WAIT_DONE, format_log_line)
from avicenna.utils.utils import DiscoveredDataset

HIT = "als_20251206_FRS005_10_1"
QUIET = "als_20251206_FRS006_04_1"
ODD = "als_20251206_FRS007_02_1"
UNLISTED = "als_20251206_FRS008_01_1"
LEFT_OUT = "als_20251206_FRS009_03_1"

_HEADER = ("Dataset", "Dataset Checks", "Rfree/Rwork", "Resolution",
           "Reference RMSD", "Map Uncertainty", "Overall", "Interesting Areas",
           "Result")


def _row(cells, tag="td"):
    return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"


def _summary(rows):
    return ("<html><table>" + _row(_HEADER, "th")
            + "".join(_row(r) for r in rows) + "</table></html>")


@pytest.fixture
def project(tmp_path):
    """A refined project analysed in place after one ground-state dataset
    (x0001).  x0002 is the hit, x0003 found nothing, x0004 was not analysed
    and x0005 is missing from the summary; one refined dataset was not in the
    run at all."""
    root = tmp_path / "project"
    root.mkdir()
    tags = [HIT, QUIET, ODD, UNLISTED, LEFT_OUT]
    info = ProjectInfo.create_from(root, [
        DiscoveredDataset(key=tag, folder_name=tag.split("_", 2)[2][:-2],
                          data_path=f"/data/{tag}", image_prefix="C6")
        for tag in tags])
    for tag in tags:
        info.update(tag, rwork="0.19", rfree="0.23",
                    high_res_limit=["1.32", "7.10", "1.32"], space_group="P43212",
                    unit_cell=["62.98", "62.98", "231.78", "90.00", "90.00", "90.00"])
        info.set_status(tag, STATUS_REFINED)
    info.update(HIT, SMILES="NC(=O)CN1CCCC1=O", library_code="EE_1", well_code="M07")
    info.update(QUIET, library_code="DSI_1", well_code="B12")
    for i, tag in enumerate([HIT, QUIET, ODD, UNLISTED], start=2):
        info[tag]["pandda_dtag"] = f"x{i:04d}"
    info.write()

    pandda = root / "pandda"
    (pandda / "analyses" / "html_summaries").mkdir(parents=True)
    for dtag, ligands in (("x0001", []), ("x0002", ["EE_1_M07"]),
                          ("x0003", ["DSI_1_B12_S", "DSI_1_B12_R"]),
                          ("x0004", []), ("x0005", [])):
        folder = pandda / "data" / f"VIC_{dtag}"
        folder.mkdir(parents=True)
        for name in ligands:
            (folder / f"{name}.cif").write_text("cif\n")
            (folder / f"{name}.pdb").write_text("HETATM\n")
    (pandda / "analyses" / "html_summaries" / "pandda_analyse.html").write_text(
        _summary([
            ("x0001", "OK", "0.22/0.19", "1.40", "0.05", "0.09", "Analysed", "", ""),
            ("x0002", "OK", "0.23/0.19", "1.32", "0.05", "0.09", "Analysed",
             "2 Clusters", "Z-Blobs Found"),
            ("x0003", "OK", "0.24/0.20", "1.50", "0.05", "0.09", "Analysed", "", ""),
            ("x0004", "Different Space Group to Reference", "0.58/0.58", "1.57",
             "nan", "nan", "Not Analysed", "", ""),
        ]))
    (pandda / "pandda.done").write_text("")
    return root


def gathered(project):
    info = ProjectInfo.load(project)
    report = AnalysisGather(project).run(info)
    return info, report


# --------------------------------------------------------------------------- #
# The gather                                                                   #
# --------------------------------------------------------------------------- #

def test_a_hit_is_recorded_with_its_events(project):
    info, _ = gathered(project)
    assert info.status_of(HIT) == STATUS_ANALYZED
    assert info[HIT]["event_found"] is True
    assert info[HIT]["n_events"] == 2
    assert info[HIT]["pandda_dtag"] == "x0002"
    assert info[HIT]["rfree"] == "0.23"             # refinement's fields stay


def test_a_dataset_with_nothing_found_is_still_analysed(project):
    info, _ = gathered(project)
    assert info.status_of(QUIET) == STATUS_ANALYZED
    assert info[QUIET]["event_found"] is False
    assert info[QUIET]["n_events"] == 0


def test_a_dataset_panDDA_did_not_analyse_says_why(project):
    info, _ = gathered(project)
    assert info.status_of(ODD) == STATUS_FAILED
    assert info[ODD][FAILED_STAGE] == STAGE_ANALYSIS
    assert "Different Space Group" in info[ODD]["failure_reason"]


def test_a_dataset_missing_from_the_summary_is_still_accounted_for(project):
    info, _ = gathered(project)
    assert info.status_of(UNLISTED) == STATUS_FAILED
    assert "x0005" in info[UNLISTED]["failure_reason"]


def test_a_dataset_not_in_the_run_is_not_recorded(project):
    info, report = gathered(project)
    assert info.status_of(LEFT_OUT) == STATUS_REFINED
    assert "pandda_dtag" not in info[LEFT_OUT]
    assert len(report.decisions) == 4
    assert report.summary() == "2 of 4 datasets analysed, 1 with events"


def test_the_ligands_placed_are_recorded(project):
    info, _ = gathered(project)
    assert info[HIT]["ligand_codes"] == ["EE_1_M07"]
    assert info[QUIET]["ligand_codes"] == ["DSI_1_B12_R", "DSI_1_B12_S"]
    assert info[ODD]["ligand_codes"] == []


# --------------------------------------------------------------------------- #
# info.csv                                                                     #
# --------------------------------------------------------------------------- #

def _info_csv(project):
    import csv
    AnalysisSentinel(project, project).on_complete()
    with open(project / "info.csv") as fh:
        return list(csv.DictReader(fh))


def test_info_csv_lists_each_analysed_dataset(project):
    rows = _info_csv(project)
    assert [row["dataset"] for row in rows] == ["x0002", "x0003"]
    assert rows[0] == {"dataset": "x0002", "resolution": "1.32",
                       "ligand_name": "EE_1_M07",
                       "a": "62.98", "b": "62.98", "c": "231.78",
                       "alpha": "90.00", "beta": "90.00", "gamma": "90.00",
                       "space_group": "P43212", "smiles": "NC(=O)CN1CCCC1=O"}


def test_a_ligand_name_carries_no_isomer_suffix(project):
    rows = {row["dataset"]: row for row in _info_csv(project)}
    assert rows["x0003"]["ligand_name"] == "DSI_1_B12"
    assert rows["x0003"]["smiles"] == ""        # optional


def test_the_info_csv_header_names_the_columns(project):
    AnalysisSentinel(project, project).on_complete()
    header = (project / "info.csv").read_text().splitlines()[0]
    assert header == ("dataset,resolution,ligand_name,a,b,c,alpha,beta,gamma,"
                      "space_group,smiles")


# --------------------------------------------------------------------------- #
# The sentinel                                                                 #
# --------------------------------------------------------------------------- #

def test_the_sentinel_writes_the_record(project):
    AnalysisSentinel(project, project).on_complete()

    info = ProjectInfo.load(project)
    assert info.status_of(HIT) == STATUS_ANALYZED
    log = (project / "analysis_pipeline.log").read_text()
    assert "2 of 4 datasets analysed" in log


def test_an_unfinished_run_is_recorded_and_then_failed(project):
    (project / "pandda" / "pandda.done").unlink()

    with pytest.raises(RuntimeError, match="did not finish"):
        AnalysisSentinel(project, project).on_complete()

    assert ProjectInfo.load(project).status_of(HIT) == STATUS_FAILED


def test_no_record_is_a_warning_not_a_failure(project):
    (project / "info.json").unlink()

    AnalysisSentinel(project, project).on_complete()

    assert "analysis results not recorded" in \
        (project / "analysis_pipeline.log").read_text()


def test_following_prints_the_pandda_log_and_the_outcome(project, monkeypatch,
                                                         capsys):
    sentinel = AnalysisSentinel(project, project)
    sentinel.FOLLOW_POLL = 0
    logs = project / "pandda" / "logs"
    logs.mkdir()
    (logs / "pandda-2026-10-01.log").write_text("Datasets Analysed: 4/4\n")

    def sentinel_finishes(_seconds):
        # The sentinel writes its outcome while the foreground is following.
        sentinel.omnibus.write_text(
            format_log_line("DONE", SENTINEL_DONE, context=SENTINEL_CONTEXT))

    monkeypatch.setattr("avicenna.pipelines.sentinel.time.sleep", sentinel_finishes)

    assert sentinel.follow() == SENTINEL_WAIT_DONE

    out = capsys.readouterr().out
    assert "Datasets Analysed: 4/4" in out
    assert SENTINEL_DONE in out


def test_following_gives_up_on_a_log_that_never_appears(project, monkeypatch, capsys):
    sentinel = AnalysisSentinel(project, project)
    sentinel.FOLLOW_POLL = 0
    clock = iter([0, 0, 120])
    monkeypatch.setattr("avicenna.pipelines.sentinel.time.time", lambda: next(clock))

    assert sentinel.follow(log_time_out=1) is None
    assert "Timed out" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Undoing the record                                                           #
# --------------------------------------------------------------------------- #

def test_overwriting_the_analysis_takes_its_results_out(project):
    AnalysisSentinel(project, project).on_complete()
    assert (project / "info.csv").is_file()

    _existing_results(project, overwrite=True, source=project)

    info = ProjectInfo.load(project)
    for tag in (HIT, QUIET, ODD, UNLISTED):
        assert info.status_of(tag) == STATUS_REFINED
        assert info[tag]["failure_reason"] is None
        for name in ("event_found", "n_events", "pandda_dtag", "ligand_codes",
                     FAILED_STAGE):
            assert name not in info[tag], (tag, name)
        assert info[tag]["rfree"] == "0.23"
    assert not (project / "pandda").exists()
    assert not (project / "info.csv").exists()


def test_resetting_analysis_leaves_refinement_failures_alone(project):
    info, _ = gathered(project)
    info.fail(QUIET, "2_refmac: refmac stopped")
    info[QUIET][FAILED_STAGE] = STAGE_REFINE

    reset_analysis(info)

    assert info.status_of(QUIET) == STATUS_FAILED
    assert info[QUIET][FAILED_STAGE] == STAGE_REFINE


def test_resetting_refinement_also_undoes_analysis(project):
    info, _ = gathered(project)

    reset = reset_refinement(info)

    assert set(reset) == {HIT, QUIET, ODD, UNLISTED, LEFT_OUT}
    for tag in reset:
        assert info.status_of(tag) == STATUS_PROCESSED
        for name in ("rwork", "rfree", "event_found", "n_events",
                     "pandda_dtag", "ligand_codes", FAILED_STAGE):
            assert name not in info[tag], (tag, name)
