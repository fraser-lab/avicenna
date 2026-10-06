"""Crystal list: reading it, and lining it up with the data on disk.

A crystal list is the harvesting robot's record of which fragment went into
which crystal.  Avicenna needs five columns of it -- DestinationName,
DestinationLocation, Library, Well and SMILES -- from which a crystal code and a
ligand source directory follow directly.  Everything to do with file format
lives in ``avicenna.utils.spreadsheet``; this module is only semantics.

Matching is the part that needs care.  A crystal list is written by a robot
and data folders are named by hand, so the same crystal is spelled several ways:
``FRS001_1``, ``FRS001_01``, ``FRS_001_01_1``.  Comparing the puck and position
as INTEGERS makes the spelling irrelevant, which is what ``parse_code`` is for.
Where a code cannot be split unambiguously it is reported rather than guessed --
``FRS0011`` has no defensible reading, and a wrong guess silently attaches the
wrong ligand to a dataset, which is the one outcome worth avoiding entirely.

Both directions of failure are reported.  A data folder with no crystal list
row is a dataset that will be analysed without ligand information; a crystal
list row with no data folder is a crystal that was never collected, or was named
differently.  Neither is an error -- a run must proceed without a crystal list
at all -- but both have to be visible on the day the data arrives rather than at
panDDA time.
"""

import os
import re

from avicenna.utils.spreadsheet import SpreadsheetError, read_sheet


class CrystalListError(Exception):
    """A crystal list is unreadable, or lacks the columns avicenna needs."""


# --------------------------------------------------------------------------- #
# Diagnostic log lines                                                         #
# --------------------------------------------------------------------------- #
# One fixed-width family covering every way a dataset can fail to line up with
# the crystal list, so the different failures read as variations of one thing.
# These are printed at CLI time, before any job exists, and so are deliberately
# not the timestamped pipeline log line in utils.format_log_line.

TAG_MISSING_ENTRY = "MISSING ENTRY"
TAG_DATA_NOT_FOUND = "DATA NOT FOUND"
TAG_NO_IMAGE_DATA = "NO IMAGE DATA"
TAG_UNPARSED_CODE = "UNPARSED CODE"
TAG_PRE_SCAN = "PRE-SCAN"
TAG_REJECTED = "REJECTED"

_ALL_TAGS = (TAG_MISSING_ENTRY, TAG_DATA_NOT_FOUND,
             TAG_NO_IMAGE_DATA, TAG_UNPARSED_CODE, TAG_PRE_SCAN, TAG_REJECTED)
_TAG_WIDTH = max(len(tag) for tag in _ALL_TAGS)


def log_tag(tag, message):
    """Return one fixed-width diagnostic line: ``TAG  -- message``."""
    return f"{tag:<{_TAG_WIDTH}}  -- {message}"


# --------------------------------------------------------------------------- #
# Crystal code parsing                                                         #
# --------------------------------------------------------------------------- #

# A separator between the puck number and the position is REQUIRED.  Without one
# the split is a guess: 'FRS0011' is puck 1 position 1, or puck 001 position 1,
# or puck 0 position 11, with nothing in the string to choose between them.
# Trailing groups (a sweep or replicate number) are matched so they do not defeat
# the pattern, but they take no part in identifying the crystal.
_CODE_RE = re.compile(
    r"^(?P<alpha>[A-Za-z]+)[_-]?(?P<puck>\d+)[_-](?P<pos>\d+)(?:[_-]\d+)*$"
)


def parse_code(text):
    """Split a crystal or data-folder code into ``(alpha, puck, position)``.

    The puck and position come back as ints, so leading zeros and separator
    style stop mattering: ``FRS006_4``, ``FRS006_04`` and ``FRS_006_04_1`` all
    give ``("FRS", 6, 4)``.  Returns None when the code cannot be split
    unambiguously -- callers report that, they must not guess.
    """
    match = _CODE_RE.match((text or "").strip())
    if not match:
        return None
    return (match.group("alpha").upper(),
            int(match.group("puck")),
            int(match.group("pos")))


# --------------------------------------------------------------------------- #
# Crystal list rows                                                           #
# --------------------------------------------------------------------------- #

# Normalised column name -> the spelling to use when talking to the user.
_DISPLAY = {
    "destinationname": "DestinationName",
    "destinationlocation": "DestinationLocation",
    "library": "Library",
    "well": "Well",
    "smiles": "SMILES",
    "compoundid": "Compound ID",
}

_REQUIRED = ("destinationname", "destinationlocation", "library", "well", "smiles")
_OPTIONAL = ("compoundid",)


def _norm(text):
    """Reduce a header cell to a comparison key (case and spacing removed)."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


class CrystalListEntry(object):
    """One crystal list row, reduced to the fields avicenna acts on.

    ``row`` is the physical row number in the file so that a complaint about an
    entry can be chased in Excel.
    """

    def __init__(self, destination_name, destination_location, library="",
                 well="", smiles="", compound_id="", row=0):
        self.destination_name = destination_name
        self.destination_location = destination_location
        self.library = library
        self.well = well
        self.smiles = smiles
        self.compound_id = compound_id
        self.row = row

    @property
    def crystal_code(self):
        """The crystal code as the list spells it: ``<name>_<location>``."""
        return f"{self.destination_name}_{self.destination_location}"

    @property
    def key(self):
        """Spelling-independent identity, or None if the code cannot be split."""
        return parse_code(self.crystal_code)

    def ligand_source(self, root):
        """Directory holding this entry's ligand model and restraints.

        None when there is no ligand root configured, or when the row names no
        library or well -- a crystal can be listed without a fragment.
        """
        if not root or not self.library or not self.well:
            return None
        from avicenna.utils.ligands import ligand_name
        return os.path.join(str(root), ligand_name(self.library, self.well), "")

    def __repr__(self):
        return (f"CrystalListEntry({self.crystal_code!r}, "
                f"library={self.library!r}, well={self.well!r}, row={self.row})")


def _find_header(rows, label):
    """Return ``(header row index, {normalised name: column index})``.

    The header is found by content, not position: a crystal list may carry a
    preamble above it, columns avicenna does not use, or -- as a real file does
    -- a column with no header at all, which must be skipped rather than shift
    the ones after it.
    """
    best_index, best_found, best_names = -1, -1, []
    for index, row in enumerate(rows):
        columns = {}
        for column, text in enumerate(row):
            name = _norm(text)
            if name and name not in columns:
                columns[name] = column
        found = [name for name in _REQUIRED if name in columns]
        if len(found) == len(_REQUIRED):
            return index, {name: column for name, column in columns.items()
                           if name in _REQUIRED + _OPTIONAL}
        if len(found) > best_found:
            best_index, best_found, best_names = index, len(found), found

    missing = ", ".join(_DISPLAY[name] for name in _REQUIRED
                        if name not in best_names)
    if best_index < 0:
        raise CrystalListError(f"{label} is empty")
    raise CrystalListError(
        f"{label} has no usable header row: no row names the column(s) "
        f"{missing}. Row {best_index + 1} came closest, naming "
        f"{', '.join(_DISPLAY[n] for n in best_names) or 'none of them'}"
    )


class CrystalList(object):
    """The rows of a crystal list that avicenna can act on."""

    def __init__(self, entries, path=None, sheet=None):
        self.entries = list(entries)
        self.path = path
        self.sheet = sheet

    def __len__(self):
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    @classmethod
    def from_file(cls, path, sheet=None, backend="auto"):
        """Read a crystal list from .xlsx, .csv, .tsv or .xls."""
        try:
            rows = read_sheet(path, sheet=sheet, backend=backend)
        except SpreadsheetError as exc:
            raise CrystalListError(
                f"could not read crystal list: {exc}") from exc
        return cls.from_rows(rows, path=path, sheet=sheet)

    @classmethod
    def from_rows(cls, rows, path=None, sheet=None):
        """Build a crystal list from rows already read (see ``read_sheet``)."""
        label = os.path.basename(str(path)) if path else "crystal list"
        header_index, columns = _find_header(rows, label)

        entries = []
        for offset, row in enumerate(rows[header_index + 1:]):
            def cell(name, _row=row):
                column = columns.get(name)
                if column is None or column >= len(_row):
                    return ""
                return _row[column].strip()

            name = cell("destinationname")
            location = cell("destinationlocation")
            if not name and not location:
                continue                      # blank spacer row, not a record
            entries.append(CrystalListEntry(
                destination_name=name,
                destination_location=location,
                library=cell("library"),
                well=cell("well"),
                smiles=cell("smiles"),
                compound_id=cell("compoundid"),
                row=header_index + offset + 2,
            ))
        return cls(entries, path=path, sheet=sheet)


# --------------------------------------------------------------------------- #
# Matching crystal list rows to data folders                                  #
# --------------------------------------------------------------------------- #

class MatchResult(object):
    """How a set of data folders lined up with a set of crystal list rows."""

    def __init__(self):
        self.matches = {}              # folder name -> CrystalListEntry
        self.unmatched_folders = []    # data with no crystal list row
        self.unmatched_entries = []    # crystal list rows with no data
        self.unparsed_folders = []     # folder names that cannot be split
        self.unparsed_entries = []     # list rows whose code cannot be split
        self.duplicate_entries = []    # (later entry, entry already holding the key)

    @property
    def folder_count(self):
        return (len(self.matches) + len(self.unmatched_folders)
                + len(self.unparsed_folders))


def match_folders(entries, folder_names):
    """Line data folder names up with crystal list entries by crystal identity.

    Repeated folder names are collapsed, and the result is keyed by name.  A
    crystal list describes crystals, not collections, so a pin that was mounted
    again on another day -- the same folder name under a different transfer
    directory, which the real pin lists are full of -- is one crystal carrying
    one fragment, and must be counted and reported once.  Callers holding
    per-collection datasets look each one up by its folder name.

    Several folders may still share one entry, since a crystal collected as two
    sweeps gives two differently named datasets.  Where two rows of the list
    claim the same crystal the first wins and the second is reported: choosing
    between
    them is the user's call, not a guess this code should make.
    """
    result = MatchResult()
    folder_names = list(dict.fromkeys(folder_names))

    by_key = {}
    for entry in entries:
        key = entry.key
        if key is None:
            result.unparsed_entries.append(entry)
        elif key in by_key:
            result.duplicate_entries.append((entry, by_key[key]))
        else:
            by_key[key] = entry

    matched_keys = set()
    for name in folder_names:
        key = parse_code(name)
        if key is None:
            result.unparsed_folders.append(name)
            continue
        entry = by_key.get(key)
        if entry is None:
            result.unmatched_folders.append(name)
        else:
            result.matches[name] = entry
            matched_keys.add(key)

    result.unmatched_entries = [entry for entry in entries
                                if entry.key is not None
                                and entry.key not in matched_keys]
    return result


def _plural(count, singular, plural=None):
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def report_matches(result, cap=20, printer=print):
    """Print the matching outcome, then a one-line summary.  Returns the lines.

    Every MISSING ENTRY is printed: each one is a dataset that will be analysed
    without ligand information, and that direction costs real data.
    DATA NOT FOUND is capped, because running a deliberate subset of the pins
    turns every uncollected row of a full list into one of these, and the
    list would bury everything else.  The full lists go into info.json either
    way, so nothing is actually lost by the cap.
    """
    lines = []

    for entry, first in result.duplicate_entries:
        lines.append(log_tag(
            TAG_MISSING_ENTRY,
            f"{entry.crystal_code} appears twice on the crystal list "
            f"(rows {first.row} and {entry.row}); row {first.row} was used"))
    for name in result.unparsed_folders:
        lines.append(log_tag(
            TAG_UNPARSED_CODE,
            f"{name} (data folder) cannot be split into puck and position"))
    for entry in result.unparsed_entries:
        lines.append(log_tag(
            TAG_UNPARSED_CODE,
            f"{entry.crystal_code} (crystal list row {entry.row}) cannot be "
            f"split into puck and position"))
    for name in result.unmatched_folders:
        lines.append(log_tag(TAG_MISSING_ENTRY,
                             f"{name} not on crystal list"))

    shown = result.unmatched_entries if cap is None else result.unmatched_entries[:cap]
    for entry in shown:
        lines.append(log_tag(TAG_DATA_NOT_FOUND,
                             f"{entry.crystal_code} on crystal list, "
                             f"no data folder"))
    remaining = len(result.unmatched_entries) - len(shown)
    if remaining > 0:
        lines.append(f"{'':<{_TAG_WIDTH}}  ... and {remaining} more")

    missing = len(result.unmatched_folders) + len(result.unparsed_folders)
    lines.append(
        f"Crystal list: {len(result.matches)}/{result.folder_count} pins "
        f"matched, {_plural(missing, 'missing entry', 'missing entries')}, "
        f"{_plural(len(result.unmatched_entries), 'crystal list row')} "
        f"without data"
    )

    for line in lines:
        printer(line)
    return lines
