"""Tests for crystal list reading and crystal-code matching.

Fixtures are built here rather than read from a real experiment, so the tests
carry no dependency on data that may move.  They do reproduce the shapes a real
crystal list has: unpadded destination locations against zero-padded folder
names, a column with no header at all, and pins collected twice.
"""

import pytest

from avicenna.utils.crystal_list import (
    CrystalList, MatchResult, CrystalListEntry, CrystalListError,
    TAG_DATA_NOT_FOUND, TAG_MISSING_ENTRY, TAG_UNPARSED_CODE,
    log_tag, match_folders, parse_code, report_matches,
)

HEADER = ["DestinationName", "DestinationLocation", "", "Library",
          "Well", "SMILES", "Compound ID"]


def sheet(*records):
    """A worksheet: the header, then one row per (name, location, library, well)."""
    rows = [HEADER]
    for name, location, library, well in records:
        rows.append([name, location, "", library, well,
                     f"SMILES-{well}", f"Z{well}"])
    return rows


def crystal_list(*records):
    return CrystalList.from_rows(sheet(*records))


def report(picks, folders, **kwargs):
    lines = []
    report_matches(match_folders(picks, folders), printer=lines.append, **kwargs)
    return lines


# --------------------------------------------------------------------------- #
# parse_code                                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code", ["FRS001_1", "FRS001_01", "FRS_001_01",
                                  "FRS_001_1", "FRS-001-01", "frs001_1",
                                  "  FRS001_01  "])
def test_padding_and_separators_are_irrelevant(code):
    assert parse_code(code) == ("FRS", 1, 1)


@pytest.mark.parametrize("code,expected", [("FRS_001_01_1", ("FRS", 1, 1)),
                                           ("FRS006_04_2", ("FRS", 6, 4))])
def test_trailing_groups_are_not_the_position(code, expected):
    """A sweep or replicate number follows the position and must not shift it."""
    assert parse_code(code) == expected


def test_position_is_compared_as_an_integer():
    assert parse_code("FRS006_4") == parse_code("FRS006_04")
    assert parse_code("AVD1234_16") == ("AVD", 1234, 16)


@pytest.mark.parametrize("code", ["FRS0011", "FRS001", "FRS", "", None,
                                  "junk", "001_01"])
def test_ambiguous_and_unusable_codes_are_refused(code):
    """'FRS0011' has several readings and guessing attaches the wrong ligand."""
    assert parse_code(code) is None


# --------------------------------------------------------------------------- #
# Reading                                                                      #
# --------------------------------------------------------------------------- #

def test_reads_rows_and_builds_codes():
    entry = crystal_list(("FRS006", "4", "EE_1", "A1")).entries[0]
    assert entry.crystal_code == "FRS006_4"
    assert entry.key == ("FRS", 6, 4)
    assert entry.library == "EE_1"
    assert entry.well == "A1"
    assert entry.compound_id == "ZA1"
    assert entry.row == 2


def test_header_is_found_below_a_preamble():
    rows = [["Exported by robot"], [], HEADER,
            ["FRS006", "4", "", "EE_1", "A1", "S", "Z"]]
    picks = CrystalList.from_rows(rows)
    assert len(picks) == 1
    assert picks.entries[0].row == 4


def test_unheaded_column_does_not_shift_the_others():
    """A real crystal list carries a column with no header at all."""
    assert crystal_list(("FRS006", "4", "EE_1", "A1")).entries[0].well == "A1"


def test_blank_rows_are_skipped_but_row_numbers_still_point_into_the_file():
    rows = sheet(("FRS006", "4", "EE_1", "A1"))
    rows.insert(1, ["", "", "", "", "", "", ""])
    picks = CrystalList.from_rows(rows)
    assert len(picks) == 1
    assert picks.entries[0].row == 3


def test_missing_required_column_names_what_is_missing():
    rows = [["DestinationName", "Library", "Well", "SMILES"],
            ["FRS006", "EE_1", "A1", "S"]]
    with pytest.raises(CrystalListError, match="DestinationLocation"):
        CrystalList.from_rows(rows)


def test_empty_sheet_is_an_error_not_an_empty_crystal_list():
    with pytest.raises(CrystalListError):
        CrystalList.from_rows([])


def test_reads_a_delimited_file(tmp_path):
    path = tmp_path / "crystal_list.csv"
    path.write_text(",".join(HEADER) + "\nFRS006,4,,EE_1,A1,SMILES-A1,ZA1\n")
    picks = CrystalList.from_file(path)
    assert len(picks) == 1
    assert picks.entries[0].crystal_code == "FRS006_4"


def test_unreadable_file_raises_crystal_list_error(tmp_path):
    with pytest.raises(CrystalListError):
        CrystalList.from_file(tmp_path / "absent.xlsx")


# --------------------------------------------------------------------------- #
# Ligand source                                                                #
# --------------------------------------------------------------------------- #

def test_ligand_source_is_named_for_library_and_well():
    entry = CrystalListEntry("FRS006", "4", library="EE_1", well="A1")
    assert entry.ligand_source("/ligands") == "/ligands/EE_1_A1/"


def test_ligand_source_absent_without_a_root_or_a_fragment():
    entry = CrystalListEntry("FRS006", "4", library="EE_1", well="A1")
    assert entry.ligand_source(None) is None
    assert CrystalListEntry("FRS006", "4").ligand_source("/ligands") is None


# --------------------------------------------------------------------------- #
# Matching                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def picks():
    return crystal_list(("FRS006", "4", "EE_1", "A1"),     # unpadded vs padded folder
                    ("FRS005", "10", "EE_1", "B2"),
                    ("FRS005", "12", "EE_1", "C3"))


def test_padding_mismatch_still_matches(picks):
    result = match_folders(picks, ["FRS006_04"])
    assert result.matches["FRS006_04"].well == "A1"
    assert result.unmatched_folders == []


def test_folder_with_no_crystal_list_row(picks):
    result = match_folders(picks, ["FRS005_11"])
    assert result.unmatched_folders == ["FRS005_11"]
    assert result.matches == {}


def test_crystal_list_row_with_no_data(picks):
    result = match_folders(picks, ["FRS006_04"])
    assert [e.crystal_code for e in result.unmatched_entries] == ["FRS005_10",
                                                                 "FRS005_12"]


def test_repeated_folder_names_are_counted_once(picks):
    """The same pin mounted again is one crystal carrying one fragment."""
    result = match_folders(picks, ["FRS006_04", "FRS006_04"])
    assert result.folder_count == 1
    assert len(result.matches) == 1


def test_repeated_unmatched_folder_is_reported_once(picks):
    result = match_folders(picks, ["FRS005_11", "FRS005_11"])
    assert result.unmatched_folders == ["FRS005_11"]


def test_two_sweeps_of_one_crystal_share_an_entry(picks):
    result = match_folders(picks, ["FRS006_04_1", "FRS006_04_2"])
    assert len(result.matches) == 2
    assert result.matches["FRS006_04_1"] is result.matches["FRS006_04_2"]


def test_unparsable_folder_is_reported_not_guessed(picks):
    result = match_folders(picks, ["FRS0064"])
    assert result.unparsed_folders == ["FRS0064"]
    assert result.matches == {}


def test_unparsable_crystal_list_row_is_reported():
    result = match_folders(crystal_list(("FRS006", "", "EE_1", "A1")), ["FRS006_04"])
    assert len(result.unparsed_entries) == 1
    assert result.unmatched_folders == ["FRS006_04"]


def test_duplicate_crystal_list_rows_first_wins_and_is_reported():
    picks = crystal_list(("FRS006", "4", "EE_1", "A1"),
                     ("FRS006", "04", "EE_1", "Z9"))
    result = match_folders(picks, ["FRS006_04"])
    assert result.matches["FRS006_04"].well == "A1"
    later, first = result.duplicate_entries[0]
    assert (later.well, first.well) == ("Z9", "A1")


def test_folder_count_covers_every_disposition():
    result = MatchResult()
    result.matches = {"a": None, "b": None}
    result.unmatched_folders = ["c"]
    result.unparsed_folders = ["d"]
    assert result.folder_count == 4


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #

def test_every_missing_entry_is_printed():
    """That direction costs real data, so none of it is capped away."""
    lines = report(crystal_list(("FRS006", "4", "EE_1", "A1")),
                   [f"FRS005_{n:02d}" for n in range(1, 31)])
    assert len([x for x in lines if x.startswith(TAG_MISSING_ENTRY)]) == 30


def test_data_not_found_is_capped_with_a_count_of_the_rest():
    picks = crystal_list(*[("FRS005", str(n), "EE_1", "A1") for n in range(1, 31)])
    lines = report(picks, ["FRS006_04"], cap=5)
    assert len([x for x in lines if x.startswith(TAG_DATA_NOT_FOUND)]) == 5
    assert any("and 25 more" in x for x in lines)


def test_cap_none_shows_everything():
    picks = crystal_list(*[("FRS005", str(n), "EE_1", "A1") for n in range(1, 31)])
    lines = report(picks, ["FRS006_04"], cap=None)
    assert len([x for x in lines if x.startswith(TAG_DATA_NOT_FOUND)]) == 30
    assert not any("more" in x for x in lines)


def test_summary_counts_both_directions():
    picks = crystal_list(("FRS006", "4", "EE_1", "A1"), ("FRS005", "10", "EE_1", "B2"))
    lines = report(picks, ["FRS006_04", "FRS005_11"])
    assert lines[-1] == ("Crystal list: 1/2 pins matched, 1 missing entry, "
                         "1 crystal list row without data")


def test_summary_pluralises():
    lines = report(crystal_list(("FRS006", "4", "EE_1", "A1")),
                   ["FRS005_10", "FRS005_11"])
    assert "2 missing entries" in lines[-1]
    assert "1 crystal list row without data" in lines[-1]


def test_unparsed_codes_reported_under_their_own_tag():
    lines = report(crystal_list(("FRS006", "4", "EE_1", "A1")), ["FRS0064"])
    assert any(x.startswith(TAG_UNPARSED_CODE) for x in lines)


def test_clean_run_reports_nothing_but_the_summary():
    lines = report(crystal_list(("FRS006", "4", "EE_1", "A1")), ["FRS006_04"])
    assert len(lines) == 1
    assert "1/1 pins matched" in lines[0]


def test_tags_line_up():
    """The point of the tag family is that the messages share a column."""
    picks = crystal_list(("FRS006", "4", "EE_1", "A1"), ("FRS005", "10", "EE_1", "B2"))
    lines = report(picks, ["FRS005_11"])
    assert len({x.index("-- ") for x in lines if "-- " in x}) == 1


def test_log_tag_pads_every_tag_to_one_width():
    widths = {len(log_tag(tag, "x").split("-- ")[0])
              for tag in (TAG_MISSING_ENTRY, TAG_DATA_NOT_FOUND, TAG_UNPARSED_CODE)}
    assert len(widths) == 1
