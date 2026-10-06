"""Tests for info.json: creation from a crystal list, the status vocabulary,
and surviving a damaged file.

The recovery tests are the ones worth having.  Everything else here would fail
loudly; a recovery bug fails by writing a well-formed file that has quietly lost
the crystal identities and every earlier stage's statistics, which reads as
success.
"""

import json

import pytest

from avicenna.utils.info import (
    FILENAME, PREV_SUFFIX, STATUS_ANALYZED, STATUS_FAILED, STATUS_IMPORTED,
    STATUS_PROCESSED, STATUS_REFINED, ProjectInfo,
)
from avicenna.utils.crystal_list import CrystalList, match_folders
from avicenna.utils.utils import DiscoveredDataset

HEADER = ["DestinationName", "DestinationLocation", "", "Library",
          "Well", "SMILES", "Compound ID"]

COLLECTION = "als_20251209"
FOLDERS = ["FRS006_04", "FRS005_10", "FRS005_11"]


def discovered(folder, sweep=1):
    """One sweep, as vic.process hands it to ProjectInfo."""
    return DiscoveredDataset(key=f"{COLLECTION}_{folder}_{sweep}",
                             folder_name=folder,
                             data_path=f"/data/{COLLECTION}_transfer/{folder}",
                             image_prefix=f"{folder}_{sweep}")


DATASETS = [discovered(name) for name in FOLDERS]
TAG_006_04, TAG_005_10, TAG_005_11 = [d.key for d in DATASETS]


@pytest.fixture
def picks():
    rows = [HEADER,
            ["FRS006", "4", "", "EE_1", "A1", "SMILES-A1", "ZA1"],
            ["FRS005", "10", "", "EE_1", "B2", "SMILES-B2", "ZB2"],
            ["FRS005", "99", "", "EE_1", "C3", "SMILES-C3", "ZC3"]]
    return CrystalList.from_rows(rows)


@pytest.fixture
def info(tmp_path, picks):
    result = match_folders(picks, FOLDERS)
    return ProjectInfo.create_from(tmp_path, DATASETS, result,
                                ligands="/ligands", crystal_list="/tmp/master.xlsx")


def bad_files(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if ".bad." in p.name)


# --------------------------------------------------------------------------- #
# Creation                                                                     #
# --------------------------------------------------------------------------- #

def test_matched_dataset_carries_its_fragment(info):
    record = info[TAG_006_04]
    assert record["crystal_code"] == "FRS006_4"
    assert record["dataset_code"] == TAG_006_04
    assert record["library_code"] == "EE_1"
    assert record["well_code"] == "A1"
    assert record["SMILES"] == "SMILES-A1"
    assert record["ligand_source"] == "/ligands/EE_1_A1/"


def test_unmatched_dataset_is_still_a_dataset(info):
    """No crystal list row means no fragment, not an excluded dataset."""
    record = info[TAG_005_11]
    assert record["crystal_code"] == "FRS005_11"
    assert "SMILES" not in record
    assert "ligand_source" not in record


def test_datasets_follow_pin_order(info):
    assert list(info.datasets) == [TAG_006_04, TAG_005_10, TAG_005_11]


def test_the_key_is_the_dataset_tag_not_the_folder(info):
    """The tag is what every later stage matches on, so it is the key."""
    assert "FRS006_04" not in info
    record = info[TAG_006_04]
    assert record["data_path"].endswith("/FRS006_04")
    assert record["image_prefix"] == "FRS006_04_1"


def test_two_sweeps_of_one_pin_are_two_datasets(tmp_path):
    """They share a folder, so only the prefix tells them apart."""
    sweeps = [discovered("FRS006_04", 1), discovered("FRS006_04", 2)]
    info = ProjectInfo.create_from(tmp_path, sweeps)
    assert len(info) == 2
    one, two = (info[d.key] for d in sweeps)
    assert one["data_path"] == two["data_path"]
    assert one["image_prefix"] != two["image_prefix"]


def test_both_directions_of_mismatch_are_recorded_in_full(info):
    unmatched = info.meta["unmatched"]
    assert unmatched["folders"] == ["FRS005_11"]
    assert unmatched["entries"] == ["FRS005_99"]


def test_paths_are_absolute_in_meta(info):
    assert info.meta["crystal_list"].startswith("/")
    assert info.meta["ligands"] == "/ligands"


def test_works_with_no_crystal_list_at_all(tmp_path):
    """info.json must never be a precondition for running."""
    info = ProjectInfo.create_from(tmp_path, DATASETS)
    assert len(info) == 3
    assert set(info[TAG_005_10]) == {"crystal_code", "dataset_code", "data_path",
                                     "image_prefix", "status", "failure_reason"}
    assert "unmatched" not in info.meta


# --------------------------------------------------------------------------- #
# Update and rename                                                            #
# --------------------------------------------------------------------------- #

def test_none_values_are_dropped(info):
    info.update(TAG_006_04, Rfree=0.23, space_group=None)
    assert info[TAG_006_04]["Rfree"] == 0.23
    assert "space_group" not in info[TAG_006_04]


def test_crystal_list_fields_are_set_once(info, capsys):
    info.update(TAG_006_04, crystal_code="WRONG_99")
    assert info[TAG_006_04]["crystal_code"] == "FRS006_4"
    assert "refusing to change" in capsys.readouterr().err


def test_update_creates_a_dataset_that_was_not_there(info):
    info.update("FRS007_01", Rfree=0.3)
    assert info["FRS007_01"]["Rfree"] == 0.3


def test_rename_carries_the_record_and_updates_the_code(info):
    info.rename(TAG_006_04, "als_20251209_FRS006_04_9")
    assert TAG_006_04 not in info
    record = info["als_20251209_FRS006_04_9"]
    assert record["dataset_code"] == "als_20251209_FRS006_04_9"
    assert record["crystal_code"] == "FRS006_4"


# --------------------------------------------------------------------------- #
# Status                                                                       #
# --------------------------------------------------------------------------- #

def test_datasets_start_imported(info):
    assert info.status_of(TAG_006_04) == STATUS_IMPORTED
    assert info[TAG_006_04]["failure_reason"] is None


def test_failure_reason_is_kept_only_while_failed(info):
    info.fail(TAG_005_10, "superseded by als_20251209_FRS005_10_1")
    assert info[TAG_005_10]["failure_reason"].startswith("superseded")
    info.set_status(TAG_005_10, STATUS_PROCESSED)
    assert info[TAG_005_10]["failure_reason"] is None


def test_select_omits_failed_datasets(info):
    info.fail(TAG_005_11, "no diffraction")
    assert info.select() == [TAG_006_04, TAG_005_10]


def test_select_by_stage_accepts_anything_further_along(info):
    """A dataset does not stop being processed once it has been refined."""
    info.set_status(TAG_006_04, STATUS_PROCESSED)
    info.set_status(TAG_005_10, STATUS_ANALYZED)
    assert info.select(stage=STATUS_PROCESSED) == [TAG_006_04, TAG_005_10]
    assert info.select(stage=STATUS_REFINED) == [TAG_005_10]


def test_failures_reports_every_reason(info):
    info.fail(TAG_005_11, "no diffraction")
    assert info.failures() == {TAG_005_11: "no diffraction"}


def test_unknown_status_is_refused(info):
    with pytest.raises(ValueError, match="unknown status"):
        info.set_status(TAG_006_04, "bogus")


def test_failing_without_a_reason_is_refused(info):
    with pytest.raises(ValueError, match="failure_reason"):
        info.set_status(TAG_006_04, STATUS_FAILED)


def test_hand_edited_typo_is_skipped_not_admitted(info, capsys):
    """A typo must not quietly let a dataset back into a generated list."""
    info.datasets[TAG_006_04]["status"] = "porcessed"
    assert TAG_006_04 not in info.select()
    assert "unrecognised status" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Writing and loading                                                          #
# --------------------------------------------------------------------------- #

def test_round_trips(info, tmp_path):
    info.set_status(TAG_006_04, STATUS_PROCESSED)
    info.write()
    again = ProjectInfo.load(tmp_path)
    assert again.datasets == info.datasets
    assert again.status_of(TAG_006_04) == STATUS_PROCESSED


def test_accepts_a_directory_or_a_file_path(info, tmp_path):
    info.write()
    assert ProjectInfo.load(tmp_path / FILENAME).datasets == info.datasets


def test_first_write_leaves_no_previous_generation(info, tmp_path):
    info.write()
    assert not (tmp_path / (FILENAME + PREV_SUFFIX)).exists()


def test_second_write_keeps_the_superseded_generation(info, tmp_path):
    info.write()
    info.update(TAG_006_04, Rfree=0.23)
    info.write()
    previous = json.loads((tmp_path / (FILENAME + PREV_SUFFIX)).read_text())
    assert "Rfree" not in previous["datasets"][TAG_006_04]


def test_write_leaves_no_temporary_files(info, tmp_path):
    info.write()
    info.write()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


# --------------------------------------------------------------------------- #
# Recovery                                                                     #
# --------------------------------------------------------------------------- #

def test_absent_file_is_not_a_recovery_event(tmp_path):
    fresh = ProjectInfo.load(tmp_path)
    assert len(fresh) == 0
    assert fresh.recovered is None


def test_damaged_file_recovers_the_previous_generation(info, tmp_path):
    info.write()
    info.update(TAG_006_04, Rfree=0.23)
    info.write()
    (tmp_path / FILENAME).write_text('{"version": 1, "datasets": {"FRS0')

    recovered = ProjectInfo.load(tmp_path)
    assert len(recovered) == 3
    assert recovered.recovered["from"] == FILENAME + PREV_SUFFIX
    assert len(bad_files(tmp_path)) == 1


def test_damage_never_overwrites_earlier_damage(info, tmp_path):
    """The preserved copy can be the only surviving record; do not eat it."""
    info.write()
    for text in ('{"datasets": {"FRS0', "not json at all"):
        (tmp_path / FILENAME).write_text(text)
        ProjectInfo.load(tmp_path).write()
    assert len(bad_files(tmp_path)) == 2


def test_damaged_file_with_no_previous_generation_starts_empty_but_says_so(tmp_path):
    (tmp_path / FILENAME).write_text("{]")
    recovered = ProjectInfo.load(tmp_path)
    assert len(recovered) == 0
    assert recovered.recovered["from"] is None
    assert recovered.recovered["damaged_copy"] in bad_files(tmp_path)


def test_wrong_shape_counts_as_damage(tmp_path):
    (tmp_path / FILENAME).write_text('{"datasets": "not a mapping"}')
    assert ProjectInfo.load(tmp_path).recovered is not None


def test_a_newer_format_is_used_as_it_stands(info, tmp_path, capsys):
    """Discarding someone else's data over a version number is not recovery."""
    info.data["version"] = 99
    info.write()
    loaded = ProjectInfo.load(tmp_path)
    assert len(loaded) == 3
    assert loaded.recovered is None
    assert "newer version" in capsys.readouterr().err
    assert bad_files(tmp_path) == []


def test_the_recovery_stamp_survives_into_the_rebuilt_file(info, tmp_path):
    info.write()
    (tmp_path / FILENAME).write_text("{]")
    recovered = ProjectInfo.load(tmp_path)
    recovered.write()
    assert json.loads((tmp_path / FILENAME).read_text())["meta"]["recovered"]
