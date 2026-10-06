"""Tests for frame-series discovery and the dataset tags built from it.

Dataset identity starts here: the CLI keys info.json from these sweeps before
any job exists, and the runner processes the same sweeps on the cluster.  A
divergence between the two produces keys nobody reads, which is why the tags
and the sweep numbering are pinned here rather than left to a live run.

Fixtures are synthetic trees under tmp_path.  Empty files are enough except
where the pre-scan rule reads a master file's trigger count, which needs a real
one -- and an empty file doubles as the unreadable case that rule has to
survive.
"""

import pathlib
import re

import pytest

from avicenna.utils.utils import (
    MIN_IMAGES, collection_id, dataset_tag, discover_sweeps, enumerate_datasets,
)


def cbf_series(folder, prefix, count=3):
    """Write one CBF frame series into *folder*."""
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(1, count + 1):
        (folder / f"{prefix}_{number:05d}.cbf").touch()
    return folder


def hdf5_series(folder, name, subdir="data01"):
    """Write one HDF5 master, nested the way a per-scan layout nests it.

    The file is empty, so its metadata is unreadable -- which is the case the
    pre-scan rule must treat as data.
    """
    target = folder / subdir
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}_master.h5").touch()
    return folder


def hdf5_master(folder, name, ntrigger=1, subdir="data01"):
    """Write a real Eiger-shaped master carrying a trigger count."""
    import h5py

    target = folder / subdir
    target.mkdir(parents=True, exist_ok=True)
    with h5py.File(str(target / f"{name}_master.h5"), "w") as handle:
        spec = handle.create_group("/entry/instrument/detector/detectorSpecific")
        spec["ntrigger"] = ntrigger
        spec["nimages"] = 2250 if ntrigger == 1 else 15
    return folder


def pin_line(collection, pin):
    return f"{collection},{pin}"


# --------------------------------------------------------------------------- #
# Sweeps within one pin folder                                                 #
# --------------------------------------------------------------------------- #

def test_one_series_is_one_sweep_numbered_from_one(tmp_path):
    pin = cbf_series(tmp_path / "FRS006_04", "FRS006_04_1")
    sweeps = list(discover_sweeps(pin).sweeps())
    assert [s.number for s in sweeps] == [1]
    assert sweeps[0].prefix == "FRS006_04_1"
    assert sweeps[0].image_dir == pin


def test_two_series_in_one_folder_are_two_sweeps_in_prefix_order(tmp_path):
    pin = tmp_path / "FRS006_04"
    cbf_series(pin, "FRS006_04_2")
    cbf_series(pin, "FRS006_04_1")
    sweeps = list(discover_sweeps(pin).sweeps())
    assert [(s.number, s.prefix) for s in sweeps] == [
        (1, "FRS006_04_1"), (2, "FRS006_04_2")]


def test_frame_prefix_need_not_match_the_folder(tmp_path):
    """Some beamlines name frames by puck position, not by pin code."""
    pin = cbf_series(tmp_path / "FRS050_06", "C6")
    sweep, = discover_sweeps(pin).sweeps()
    assert sweep.prefix == "C6"


def test_hdf5_master_is_its_own_sweep_with_its_own_image_dir(tmp_path):
    pin = hdf5_series(tmp_path / "FRS-001_01", "park-x00828_1578")
    found = discover_sweeps(pin)
    sweep, = found.sweeps()
    assert found.data_format == "hdf5"
    assert sweep.prefix == "park-x00828_1578"
    assert sweep.image_dir == pin / "data01"


def test_hdf5_wins_over_stray_cbf_files(tmp_path):
    pin = hdf5_series(tmp_path / "FRS-001_01", "park-x00828_1578")
    cbf_series(pin, "leftover")
    assert discover_sweeps(pin).data_format == "hdf5"


# --------------------------------------------------------------------------- #
# Folders that yield nothing                                                   #
# --------------------------------------------------------------------------- #

def test_missing_folder_says_so(tmp_path):
    found = discover_sweeps(tmp_path / "NOT_THERE")
    assert not found
    assert "image directory not found" in found.reason


def test_empty_folder_says_so(tmp_path):
    pin = tmp_path / "FRS006_04"
    pin.mkdir()
    found = discover_sweeps(pin)
    assert not found
    assert "no diffraction data found" in found.reason


@pytest.mark.parametrize("name, subdir", [
    ("scan_r_Raster_1", "."),
    ("scan", "rasterImages"),
])
def test_raster_pre_scans_are_not_data(tmp_path, name, subdir):
    """A pre-scan folder is not an empty one, and the reason must say which."""
    pin = hdf5_series(tmp_path / "FRS006_04", name, subdir=subdir)
    found = discover_sweeps(pin)
    assert not found
    assert "pre-scans" in found.reason
    assert [reason for _prefix, reason in found.prescans] == ["raster pre-scan"]


# --------------------------------------------------------------------------- #
# Pre-scans, by metadata rather than by name                                   #
# --------------------------------------------------------------------------- #
# A pre-scan that reaches XDS wastes one job, which is visible in the log.  One
# that reaches sweep NUMBERING is worse: it takes sweep 1, the real collection
# becomes sweep 2, and that number is part of a key nothing re-writes later.

def test_a_multi_trigger_series_is_a_pre_scan_whatever_it_is_named(tmp_path):
    """The beamline's own names are not a signal; one trigger per point is."""
    pin = tmp_path / "FRS-001_03"
    hdf5_master(pin, "anything_1581", ntrigger=11, subdir="raster01")
    found = discover_sweeps(pin)
    assert not found
    assert found.prescans == [("anything_1581", "grid pre-scan (11 triggers)")]


def test_a_pre_scan_does_not_take_the_real_collections_sweep_number(tmp_path):
    pin = tmp_path / "FRS-001_03"
    # Sorts FIRST, so without the filter it would be sweep 1.
    hdf5_master(pin, "aaa_raster_1581", ntrigger=11, subdir="raster01")
    hdf5_master(pin, "park-dj1-x00830_1583", ntrigger=1, subdir="data01")

    sweeps = list(discover_sweeps(pin).sweeps())
    assert [(s.number, s.prefix) for s in sweeps] == [
        (1, "park-dj1-x00830_1583")]


def test_unreadable_metadata_means_data(tmp_path):
    """A wasted job is visible in the log; a dropped dataset is not."""
    pin = hdf5_series(tmp_path / "FRS-001_03", "park-dj1-x00830_1583")
    found = discover_sweeps(pin)
    assert found.prescans == []
    assert [s.prefix for s in found.sweeps()] == ["park-dj1-x00830_1583"]


def test_a_snapshot_beside_a_full_cbf_sweep_is_a_pre_scan(tmp_path):
    """A one-frame centring shot sorts first and would otherwise be sweep 1."""
    pin = tmp_path / "AML012_06-2"
    cbf_series(pin, "AML012_06-2_0", count=1)
    cbf_series(pin, "AML012_06-2_1", count=MIN_IMAGES + 1)

    found = discover_sweeps(pin)
    assert [(s.number, s.prefix) for s in found.sweeps()] == [
        (1, "AML012_06-2_1")]
    assert [prefix for prefix, _reason in found.prescans] == ["AML012_06-2_0"]


def test_a_short_cbf_series_on_its_own_is_kept(tmp_path):
    """With no full sweep to compare against it may be an aborted collection."""
    pin = cbf_series(tmp_path / "FRS006_04", "FRS006_04_1", count=12)
    found = discover_sweeps(pin)
    assert [s.prefix for s in found.sweeps()] == ["FRS006_04_1"]
    assert found.prescans == []


def test_two_short_cbf_series_with_no_full_sweep_are_both_kept(tmp_path):
    pin = tmp_path / "FRS006_04"
    cbf_series(pin, "FRS006_04_1", count=12)
    cbf_series(pin, "FRS006_04_2", count=30)
    found = discover_sweeps(pin)
    assert len(list(found.sweeps())) == 2
    assert found.prescans == []


def test_skip_prescans_false_keeps_everything(tmp_path):
    """The escape hatch for a collection that really does use many triggers."""
    pin = tmp_path / "FRS-001_03"
    hdf5_master(pin, "aaa_raster_1581", ntrigger=11, subdir="raster01")
    hdf5_master(pin, "park-dj1-x00830_1583", ntrigger=1, subdir="data01")

    found = discover_sweeps(pin, skip_prescans=False)
    assert [s.number for s in found.sweeps()] == [1, 2]
    assert found.prescans == []


# --------------------------------------------------------------------------- #
# Tags                                                                         #
# --------------------------------------------------------------------------- #

def test_collection_id_drops_the_transfer_suffix():
    assert collection_id("/data/als_20251206_transfer") == "als_20251206"


def test_dataset_tag_is_collection_pin_sweep():
    assert dataset_tag("/data/als_20251206_transfer", "FRS006_04", 2) == \
        "als_20251206_FRS006_04_2"


# --------------------------------------------------------------------------- #
# Pin list -> datasets                                                         #
# --------------------------------------------------------------------------- #

def test_pin_list_expands_to_one_dataset_per_sweep(tmp_path):
    collection = tmp_path / "als_20251206_transfer"
    cbf_series(collection / "FRS006_04", "FRS006_04_1")
    cbf_series(collection / "FRS005_10", "FRS005_10_1")
    cbf_series(collection / "FRS005_10", "FRS005_10_2")

    datasets, empty, prescans = enumerate_datasets(
        [pin_line(collection, "FRS006_04"), pin_line(collection, "FRS005_10")])
    assert empty == [] and prescans == []
    assert [d.key for d in datasets] == ["als_20251206_FRS006_04_1",
                                         "als_20251206_FRS005_10_1",
                                         "als_20251206_FRS005_10_2"]


def test_two_sweeps_share_a_folder_and_differ_only_by_prefix(tmp_path):
    collection = tmp_path / "als_20251206_transfer"
    cbf_series(collection / "FRS005_10", "FRS005_10_1")
    cbf_series(collection / "FRS005_10", "FRS005_10_2")

    first, second = enumerate_datasets([pin_line(collection, "FRS005_10")])[0]
    assert first.data_path == second.data_path
    assert first.folder_name == second.folder_name == "FRS005_10"
    assert first.image_prefix != second.image_prefix


def test_a_recollected_pin_is_two_datasets_of_one_crystal(tmp_path):
    """The same folder name under two transfers: two keys, one crystal."""
    monday = tmp_path / "als_20251206_transfer"
    friday = tmp_path / "als_20251209_transfer"
    cbf_series(monday / "FRS006_04", "FRS006_04_1")
    cbf_series(friday / "FRS006_04", "FRS006_04_1")

    datasets, _empty, _pre = enumerate_datasets([pin_line(monday, "FRS006_04"),
                                                 pin_line(friday, "FRS006_04")])
    assert [d.key for d in datasets] == ["als_20251206_FRS006_04_1",
                                         "als_20251209_FRS006_04_1"]
    assert {d.folder_name for d in datasets} == {"FRS006_04"}
    assert datasets[0].data_path != datasets[1].data_path


def test_data_path_recovers_the_folder_name(tmp_path):
    """The tag reformats the folder name away and cannot be split back apart."""
    collection = tmp_path / "ssrl_20260522_transfer"
    cbf_series(collection / "FRS050_06", "C6")

    dataset, = enumerate_datasets([pin_line(collection, "FRS050_06")])[0]
    assert pathlib.Path(dataset.data_path).name == "FRS050_06"
    assert dataset.image_prefix == "C6"


def test_frameless_folder_is_reported_and_not_keyed(tmp_path):
    collection = tmp_path / "als_20251206_transfer"
    cbf_series(collection / "FRS006_04", "FRS006_04_1")
    (collection / "FRS005_10").mkdir()

    datasets, empty, _pre = enumerate_datasets(
        [pin_line(collection, "FRS006_04"), pin_line(collection, "FRS005_10")])
    assert [d.key for d in datasets] == ["als_20251206_FRS006_04_1"]
    assert [folder for folder, _reason in empty] == ["FRS005_10"]


def test_a_data_range_column_does_not_disturb_the_line(tmp_path):
    collection = tmp_path / "als_20251206_transfer"
    cbf_series(collection / "FRS006_04", "FRS006_04_1")
    datasets, _empty, _pre = enumerate_datasets([f"{collection},FRS006_04,1-900"])
    assert [d.key for d in datasets] == ["als_20251206_FRS006_04_1"]


# --------------------------------------------------------------------------- #
# One copy of the logic, not two                                               #
# --------------------------------------------------------------------------- #

def test_the_runner_has_no_frame_discovery_of_its_own():
    """The runner must take its sweeps from the shared helper.

    A second copy of the discovery inside the runner is the failure this whole
    arrangement exists to prevent, and it would be invisible until the tags
    silently disagreed.  Guarded structurally because the runner's own tags are
    only produced on the way to submitting an SGE array.
    """
    from avicenna.pipelines import processing_runner

    source = pathlib.Path(processing_runner.__file__).read_text()
    assert "discover_sweeps(" in source, "the runner no longer uses the helper"
    assert "rglob(" not in source, "recursive frame discovery is back in the runner"

    stray = [call for call in re.findall(r"(\w+)\.glob\(", source)
             if call != "xds_dir"]
    assert not stray, f"frame globbing in the runner: {sorted(set(stray))}"
