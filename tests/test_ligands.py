"""Tests for ligand lookup, placement and generation in the analysis stage.

A ligand library holds one folder per ligand, named <library>_<well>, plus one
per stereoisomer.  The cases worth pinning are which folders a well matches,
where a dataset's ligand files end up, the ligands that have to be generated
with phenix.elbow inside the pandda job, and the ligand_library filter.
"""

import json
import stat

import pytest

from avicenna.utils import ligands
from avicenna.utils.ligands import find_ligand_files, generate
from avicenna.utils.phil.analysis import pandda_preparer
from avicenna.utils.phil.analysis.pandda_preparer import PanDDAPreparer
from avicenna.utils.info import STATUS_REFINED, ProjectInfo
from avicenna.utils.utils import DiscoveredDataset

HIT = "als_20251206_FRS005_10_1"
ISOMERS = "als_20251206_FRS006_04_1"
NEW = "als_20251206_FRS007_02_1"
BARE = "als_20251206_FRS008_01_1"
OTHER_LIB = "als_20251206_FRS009_03_1"


def _ligand(root, name):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / f"{name}.cif").write_text("cif\n")
    (folder / f"{name}.pdb").write_text("HETATM\n")
    return folder


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "pdb_final_geometry"
    _ligand(root, "EE_1_M07")
    _ligand(root, "DSI_1_B12_R")
    _ligand(root, "DSI_1_B12_S")
    _ligand(root, "EE_1_M071")              # another well's prefix, no separator
    return root


# --------------------------------------------------------------------------- #
# Lookup                                                                       #
# --------------------------------------------------------------------------- #

def test_a_well_finds_its_own_folder(library):
    pairs = find_ligand_files(library, "EE_1", "M07")
    assert [cif.name for cif, _ in pairs] == ["EE_1_M07.cif"]


def test_every_stereoisomer_is_found(library):
    pairs = find_ligand_files(library, "DSI_1", "B12")
    assert [cif.name for cif, _ in pairs] == ["DSI_1_B12_R.cif", "DSI_1_B12_S.cif"]


def test_restraints_without_a_model_are_not_a_ligand(library):
    (library / "EE_1_M07" / "EE_1_M07.pdb").unlink()
    assert find_ligand_files(library, "EE_1", "M07") == []


def test_no_root_or_no_fragment_finds_nothing(library):
    assert find_ligand_files(None, "EE_1", "M07") == []
    assert find_ligand_files(library, "", "M07") == []


# --------------------------------------------------------------------------- #
# Placement                                                                    #
# --------------------------------------------------------------------------- #

@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for tag in (HIT, ISOMERS, NEW, BARE, OTHER_LIB):
        job = root / "qrefine" / tag
        job.mkdir(parents=True)
        (job / "final_refined.pdb").write_text("ATOM\n")
        (job / "final_refined.mtz").write_text("x\n")
    return root


RECORDS = {
    HIT:       {"crystal_code": "FRS005_10", "library_code": "EE_1", "well_code": "M07"},
    ISOMERS:   {"crystal_code": "FRS006_04", "library_code": "DSI_1", "well_code": "B12"},
    NEW:       {"crystal_code": "FRS007_02", "library_code": "EE_1", "well_code": "A03",
                "SMILES": "NC(=O)CN1CCCC1=O"},
    BARE:      {"crystal_code": "FRS008_01"},
    OTHER_LIB: {"crystal_code": "FRS009_03", "library_code": "UCSF91_1",
                "well_code": "C01"},
}


@pytest.fixture
def elbow(tmp_path, monkeypatch):
    """A phenix install whose elbow writes <output>.cif and <output>.pdb."""
    bin_dir = tmp_path / "phenix" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "phenix.elbow"
    script.write_text('#!/bin/bash\nfor a in "$@"; do case $a in --output=*) '
                      'n=${a#--output=};; esac; done\n'
                      'echo cif > $n.cif; echo HETATM > $n.pdb\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr("avicenna.utils.software.find_phenix",
                        lambda *a, **k: (str(bin_dir), "2.0"))
    return script


def prepared(project, library, monkeypatch, **kw):
    """Prepare the run; returns the preparer and {dataset tag: data folder}
    for the datasets placed, read back from the panDDA codes in info.json."""
    info = ProjectInfo.create_from(project, [
        DiscoveredDataset(key=tag, folder_name=tag, data_path=f"/data/{tag}",
                          image_prefix="C6") for tag in RECORDS])
    for tag, fields in RECORDS.items():
        info.datasets[tag].update(fields)
        job = project / "qrefine" / tag
        info.update(tag, final_refined_pdb=str(job / "final_refined.pdb"),
                    final_refined_mtz=str(job / "final_refined.mtz"),
                    refine_job_dir=str(job))
        info.set_status(tag, STATUS_REFINED)
    info.write()
    monkeypatch.setattr(pandda_preparer, "qsub_terse", lambda args: None)
    preparer = PanDDAPreparer([project], cpus=4, project_dir=project,
                              records=RECORDS, ligand_source_path=library, **kw)
    preparer.run()
    data = project / "pandda" / "data"
    folders = {tag: data / f"{preparer.prefix}_{record['pandda_dtag']}"
               for tag, record in ProjectInfo.load(project).datasets.items()
               if record.get("pandda_dtag")}
    return preparer, folders


def _ligand_files(folder):
    return sorted(p.name for p in folder.iterdir() if p.suffix in (".cif",)
                  or (p.suffix == ".pdb" and p.name != "final_refined.pdb"))


def test_a_library_ligand_is_placed_beside_the_model(project, library, elbow,
                                                     monkeypatch):
    _, folders = prepared(project, library, monkeypatch)
    assert _ligand_files(folders[HIT]) == ["EE_1_M07.cif", "EE_1_M07.pdb"]


def test_every_isomer_is_placed(project, library, elbow, monkeypatch):
    _, folders = prepared(project, library, monkeypatch)
    assert _ligand_files(folders[ISOMERS]) == [
        "DSI_1_B12_R.cif", "DSI_1_B12_R.pdb", "DSI_1_B12_S.cif", "DSI_1_B12_S.pdb"]


def test_a_dataset_with_no_fragment_gets_no_ligand(project, library, elbow,
                                                   monkeypatch):
    _, folders = prepared(project, library, monkeypatch)
    assert _ligand_files(folders[BARE]) == []


def test_a_missing_ligand_is_generated_in_the_pandda_job(project, library, elbow,
                                                         monkeypatch, capsys):
    _, folders = prepared(project, library, monkeypatch)

    script = (project / "pandda" / "pandda.sh").read_text()
    assert "from avicenna.utils.ligands import generate" in script
    assert script.index("generate(") < script.index("pandda.analyse")
    todo = json.loads((project / "pandda" / "ligands_todo.json").read_text())
    assert todo["elbow"] == str(elbow)
    assert [item["name"] for item in todo["ligands"]] == ["EE_1_A03"]
    assert todo["ligands"][0]["targets"] == [str(folders[NEW])]
    assert "1 to generate with phenix.elbow" in capsys.readouterr().out


def test_without_phenix_the_dataset_is_analysed_without_a_ligand(
        project, library, monkeypatch, capsys):
    def missing(*a, **k):
        raise FileNotFoundError("phenix")
    monkeypatch.setattr("avicenna.utils.software.find_phenix", missing)

    prepared(project, library, monkeypatch)

    assert "generate(" not in (project / "pandda" / "pandda.sh").read_text()
    assert "phenix.elbow not found" in capsys.readouterr().out


def test_a_ligand_generated_earlier_is_reused(project, library, elbow, monkeypatch):
    _ligand(project / ligands.GENERATED_DIR, "EE_1_A03")

    _, folders = prepared(project, library, monkeypatch)

    assert _ligand_files(folders[NEW]) == ["EE_1_A03.cif", "EE_1_A03.pdb"]
    assert not (project / "pandda" / "ligands_todo.json").exists()


# --------------------------------------------------------------------------- #
# The library filter                                                           #
# --------------------------------------------------------------------------- #

def test_every_library_is_analysed_by_default(project, library, elbow, monkeypatch):
    _, folders = prepared(project, library, monkeypatch)
    assert OTHER_LIB in folders


def test_a_library_not_selected_is_left_out(project, library, elbow, monkeypatch,
                                            capsys):
    _, folders = prepared(project, library, monkeypatch,
                          ligand_libraries=["EE_1", "DSI_1"])

    assert OTHER_LIB not in folders
    assert BARE in folders                  # no library recorded: kept
    assert "Left out by ligand_library: 1 from UCSF91_1" in capsys.readouterr().out


def test_the_default_selection_is_no_filter():
    from avicenna.utils.phil import AnalysisPHIL
    from avicenna.utils.phil.analysis.pandda import _selected_libraries

    inp = AnalysisPHIL()._params.analysis.input
    assert _selected_libraries(inp) is None


# --------------------------------------------------------------------------- #
# Generation                                                                   #
# --------------------------------------------------------------------------- #

def _todo(tmp_path, elbow, targets):
    path = tmp_path / "todo.json"
    path.write_text(json.dumps({"elbow": str(elbow), "ligands": [{
        "name": "EE_1_A03", "smiles": "NC(=O)CN1CCCC1=O",
        "out_dir": str(tmp_path / "generated" / "EE_1_A03"),
        "targets": [str(t) for t in targets]}]}))
    return path


def test_a_generated_ligand_reaches_every_dataset_that_needs_it(tmp_path, elbow):
    targets = [tmp_path / "x0001", tmp_path / "x0002"]
    for target in targets:
        target.mkdir()

    generate(_todo(tmp_path, elbow, targets), cpus=2)

    for target in targets:
        assert sorted(p.name for p in target.iterdir()) == ["EE_1_A03.cif",
                                                            "EE_1_A03.pdb"]
    assert (tmp_path / "generated" / "EE_1_A03" / "EE_1_A03.cif").is_file()


def test_a_failed_generation_is_a_warning_not_a_failed_job(tmp_path, capsys):
    broken = tmp_path / "phenix.elbow"
    broken.write_text("#!/bin/bash\nexit 3\n")
    broken.chmod(broken.stat().st_mode | stat.S_IXUSR)
    target = tmp_path / "x0001"
    target.mkdir()

    generate(_todo(tmp_path, broken, [target]))

    assert list(target.iterdir()) == []
    assert "could not generate EE_1_A03 (exit 3)" in capsys.readouterr().out


def test_a_dataset_not_in_the_run_loses_an_earlier_panDDA_code(
        project, library, elbow, monkeypatch):
    stale = dict(RECORDS)
    stale[OTHER_LIB] = dict(RECORDS[OTHER_LIB], pandda_dtag="x0099")
    monkeypatch.setitem(globals(), "RECORDS", stale)

    _, folders = prepared(project, library, monkeypatch,
                          ligand_libraries=["EE_1", "DSI_1"])

    assert OTHER_LIB not in folders
    assert "pandda_dtag" not in ProjectInfo.load(project)[OTHER_LIB]
