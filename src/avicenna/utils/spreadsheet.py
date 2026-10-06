"""Spreadsheet reading for avicenna -- .xlsx / .csv / .tsv with no dependencies.

Avicenna needs to read exactly one kind of spreadsheet: a crystal list
exported from a harvesting robot or assembled by hand.  That is a small,
flat, text-and-numbers table, so requiring openpyxl on every machine that runs
``vic.process`` buys very little -- and costs real friction, because the conda
environment that actually runs the pipeline is not always the one a package
lands in.  An .xlsx file is a zip of XML, so the stdlib can read it.

Two backends, one contract
--------------------------
``stdlib``   parses the zip/XML directly.  Always available.
``openpyxl`` used when explicitly asked for, or automatically as a fallback if
             the stdlib parser chokes on an unusual file.

Both are normalised to the SAME output so behaviour does not depend on which
machine you are on: every cell comes back as a ``str``, blanks as ``""``, rows
padded to equal width, and physical row numbers preserved (row *n* of the file
is index ``n - 1`` of the returned list, so blank rows do not shift anything and
error messages can quote a row number the user can find in Excel).

Number and date normalisation is the part worth knowing about.  A cell holding
the integer 14 is stored as ``14`` by one backend and ``14.0`` by the other;
both are rendered ``"14"`` here, so ``int(cell)`` works either way.  Dates are
stored in the file as a serial day count, which is meaningless to a caller, so
date-formatted cells are converted and rendered ``YYYY-MM-DD``,
``YYYY-MM-DD HH:MM:SS``, or ``HH:MM:SS`` -- the stdlib backend reads the
workbook's number formats to find them, exactly as a reader would need to for
the TimeArrival / TimeDeparture / PickDuration columns of a fully populated
crystal list.

Known limits: the stdlib backend reads cell values, not styling, formulas, or
merged-cell geometry, and takes a formula cell's last cached result (openpyxl is
opened ``data_only=True`` to match).  Neither is a concern for tabular data.
"""

import csv
import datetime
import os
import re
import zipfile
from xml.etree import ElementTree as ET

# SpreadsheetML namespaces.  Sheet XML and the two flavours of relationship
# file (package-level and document-level) each get their own.
_NS      = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS  = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Built-in number-format ids that denote a date and/or a time.  Custom formats
# (id >= 164) are detected by inspecting their format code instead.
_DATE_FMT_IDS = frozenset([14, 15, 16, 17, 18, 19, 20, 21, 22,
                           27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
                           45, 46, 47,
                           50, 51, 52, 53, 54, 55, 56, 57, 58])

# The subset of those that carry a time and no date.  These have to be told
# apart from dates by FORMAT rather than by value: a bare time is stored as a
# fraction of a day, which under the 1904 date system is indistinguishable from
# a real date early in 1904.
_TIME_FMT_IDS = frozenset([18, 19, 20, 21, 45, 46, 47])

_CELL_REF_RE = re.compile(r"^([A-Za-z]+)")

# Quoted literals, escaped characters and [colour]/[condition] blocks inside a
# number-format code are not date tokens -- strip them before looking for one.
_FMT_LITERAL_RE = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]')


class SpreadsheetError(Exception):
    """A spreadsheet could not be read, or its structure is unusable."""


# --------------------------------------------------------------------------- #
# Cell-value normalisation (shared by both backends)                           #
# --------------------------------------------------------------------------- #

def column_index(cell_ref):
    """Return the 0-based column index of a cell reference (``"AB12"`` -> 27).

    Gap columns are the reason this exists: a row's ``<c>`` elements only cover
    cells that hold something, so positions have to come from the reference, not
    from counting elements.
    """
    match = _CELL_REF_RE.match(cell_ref or "")
    if not match:
        raise SpreadsheetError(f"malformed cell reference: {cell_ref!r}")
    index = 0
    for char in match.group(1).upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _number_str(text):
    """Render a numeric cell so that an integral value has no trailing ``.0``.

    ``DestinationLocation`` arrives as ``14`` from one backend and ``14.0`` from
    the other.  Both must survive ``int()``.
    """
    try:
        number = float(text)
    except (TypeError, ValueError):
        return str(text)
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


def _format_datetime(value):
    """Render a date/time as the narrowest representation that loses nothing."""
    if isinstance(value, datetime.datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, datetime.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M:%S")
    return str(value)


def _split_serial(serial):
    """Split an Excel serial into (whole days, time-of-day).

    The time is rounded to the nearest second.  Binary fractions of a day do not
    land on exact seconds -- 02:30 is stored as 0.10416666666666667 -- and
    without rounding a midnight date picks up stray microseconds and stops
    rendering as a bare date.
    """
    days = int(serial // 1)
    seconds = int(round((serial - days) * 86400))
    if seconds >= 86400:          # rounded up through midnight
        days += 1
        seconds -= 86400
    return days, (datetime.datetime.min + datetime.timedelta(seconds=seconds)).time()


def _serial_to_datetime(serial, date1904=False):
    """Convert an Excel serial day number to a datetime.

    The 1900 system counts 1900 as a leap year, which it was not, so every
    serial at or past the phantom 29 February 1900 is one day ahead of a real
    day count.  Shifting the epoch back a day for those serials is the standard
    correction.  Serial 60 *is* the phantom day and has no real date -- it
    collapses onto 28 February, the same lossy answer every other reader gives.
    """
    days, time_of_day = _split_serial(serial)
    if date1904:
        epoch = datetime.date(1904, 1, 1)
    else:
        epoch = datetime.date(1899, 12, 30 if serial >= 60 else 31)
    try:
        return datetime.datetime.combine(epoch + datetime.timedelta(days=days),
                                         time_of_day)
    except (OverflowError, ValueError):
        raise SpreadsheetError(f"date serial out of range: {serial!r}")


def _serial_to_time(serial):
    """Convert an Excel serial to a bare time of day, discarding whole days."""
    return _split_serial(serial)[1]


def _is_date_format(code):
    """True if a number-format code renders a date and/or a time."""
    if not code:
        return False
    stripped = _FMT_LITERAL_RE.sub("", code)
    return any(token in stripped for token in "yYmMdDhHsS")


def _is_time_only_format(code):
    """True if a number-format code renders a time and no date.

    ``m`` is deliberately not treated as a date token here: in a format code it
    means minutes as often as it means months, so only an unambiguous ``y`` or
    ``d`` rules a time-only format out.
    """
    if not code:
        return False
    stripped = _FMT_LITERAL_RE.sub("", code)
    if any(token in stripped for token in "yYdD"):
        return False
    return any(token in stripped for token in "hHsS")


# --------------------------------------------------------------------------- #
# stdlib .xlsx backend                                                         #
# --------------------------------------------------------------------------- #

def _xlsx_shared_strings(archive):
    """Return the shared-string table, or [] when the workbook has none.

    Small workbooks written by robot software frequently store every string
    inline and ship no sharedStrings.xml at all, so its absence is normal and
    must not be an error.  Rich-text runs are flattened by concatenating runs.
    """
    try:
        data = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    return ["".join(node.text or "" for node in item.iter(_NS + "t"))
            for item in root.iter(_NS + "si")]


def _xlsx_date_styles(archive):
    """Return (date style indices, time-only style indices).

    A cell's ``s`` attribute indexes into cellXfs, each entry of which names a
    number format -- built in, or defined in the same file for ids >= 164.  That
    format is the only thing distinguishing a date from an ordinary number,
    since both are stored as a bare serial.
    """
    try:
        styles = ET.fromstring(archive.read("xl/styles.xml"))
    except KeyError:
        return frozenset(), frozenset()

    custom = {}
    for node in styles.iter(_NS + "numFmt"):
        try:
            custom[int(node.get("numFmtId"))] = node.get("formatCode") or ""
        except (TypeError, ValueError):
            continue

    cell_xfs = styles.find(_NS + "cellXfs")
    if cell_xfs is None:
        return frozenset(), frozenset()

    date_styles, time_styles = set(), set()
    for index, xf in enumerate(cell_xfs.findall(_NS + "xf")):
        try:
            fmt_id = int(xf.get("numFmtId", 0))
        except (TypeError, ValueError):
            continue
        code = custom.get(fmt_id)
        if fmt_id in _DATE_FMT_IDS or _is_date_format(code):
            date_styles.add(index)
            if fmt_id in _TIME_FMT_IDS or (code and _is_time_only_format(code)):
                time_styles.add(index)
    return frozenset(date_styles), frozenset(time_styles)


def _xlsx_sheets(archive):
    """Return [(sheet name, archive member path)] in workbook order.

    Sheet order in workbook.xml is what the user sees in Excel, and it is not
    necessarily the sheetN.xml numbering, so the relationship id has to be
    resolved rather than guessed.
    """
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    except KeyError:
        raise SpreadsheetError("not a valid .xlsx file: xl/workbook.xml is missing")

    targets = {}
    try:
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        rels = None
    if rels is not None:
        for rel in rels.iter(_PKG_REL + "Relationship"):
            target = rel.get("Target") or ""
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = "xl/" + target
            targets[rel.get("Id")] = target

    sheets = []
    for node in workbook.iter(_NS + "sheet"):
        name = node.get("name") or ""
        path = targets.get(node.get(_REL_NS + "id"))
        if path and path in archive.namelist():
            sheets.append((name, path))
    if not sheets:
        # No usable relationships -- fall back to the conventional layout.
        sheets = [(name, path) for name, path in
                  ((n, f"xl/worksheets/sheet{i}.xml")
                   for i, n in enumerate([s.get("name") or "" for s in
                                          workbook.iter(_NS + "sheet")], start=1))
                  if path in archive.namelist()]
    if not sheets:
        raise SpreadsheetError("workbook contains no readable worksheets")
    return sheets


def _xlsx_date1904(archive):
    """True when the workbook uses the 1904 date system (older Mac Excel)."""
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    except KeyError:
        return False
    props = workbook.find(_NS + "workbookPr")
    if props is None:
        return False
    return (props.get("date1904") or "").lower() in ("1", "true")


def _xlsx_cell_text(cell, shared, date_styles, time_styles, date1904):
    """Normalise one ``<c>`` element to a string."""
    cell_type = cell.get("t")

    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(_NS + "t"))

    value = cell.find(_NS + "v")
    if value is None or value.text is None:
        return ""
    text = value.text

    if cell_type == "s":
        try:
            return shared[int(text)]
        except (ValueError, IndexError):
            raise SpreadsheetError(f"shared-string index out of range: {text!r}")
    if cell_type == "b":
        return "TRUE" if text not in ("0", "", None) else "FALSE"
    if cell_type in ("str", "e"):
        return text

    # No type attribute means a number -- which may be a date in disguise.
    try:
        style = int(cell.get("s", -1))
    except (TypeError, ValueError):
        style = -1
    if style in date_styles:
        try:
            serial = float(text)
            if style in time_styles:
                return _format_datetime(_serial_to_time(serial))
            return _format_datetime(_serial_to_datetime(serial, date1904))
        except (ValueError, SpreadsheetError):
            return _number_str(text)
    return _number_str(text)


def _read_xlsx_stdlib(path, sheet):
    with zipfile.ZipFile(path) as archive:
        sheets = _xlsx_sheets(archive)
        name, member = _select_sheet(sheets, sheet, path)
        shared = _xlsx_shared_strings(archive)
        date_styles, time_styles = _xlsx_date_styles(archive)
        date1904 = _xlsx_date1904(archive)
        root = ET.fromstring(archive.read(member))

    cells = {}
    max_row = 0
    max_col = -1
    for row_index, row in enumerate(root.iter(_NS + "row"), start=1):
        # Trust the row's own r attribute when present so that blank rows keep
        # their place; fall back to positional counting when it is absent.
        try:
            number = int(row.get("r", row_index))
        except (TypeError, ValueError):
            number = row_index
        for cell in row.findall(_NS + "c"):
            ref = cell.get("r")
            col = column_index(ref) if ref else 0
            text = _xlsx_cell_text(cell, shared, date_styles, time_styles, date1904)
            if text != "":
                cells[(number, col)] = text
                max_col = max(max_col, col)
        max_row = max(max_row, number)

    if max_col < 0:
        return []
    return [[cells.get((r, c), "") for c in range(max_col + 1)]
            for r in range(1, max_row + 1)]


# --------------------------------------------------------------------------- #
# openpyxl .xlsx backend                                                        #
# --------------------------------------------------------------------------- #

def _read_xlsx_openpyxl(path, sheet):
    try:
        import openpyxl
    except ImportError:
        raise SpreadsheetError(
            "the openpyxl backend was requested but openpyxl is not installed"
        )

    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except SpreadsheetError:
        raise
    except Exception as exc:
        raise SpreadsheetError(
            f"openpyxl could not open {os.path.basename(path)}: {exc}"
        ) from exc

    try:
        names = list(workbook.sheetnames)
        name, _ = _select_sheet([(n, n) for n in names], sheet, path)
        rows = [[_openpyxl_cell_text(v) for v in row]
                for row in workbook[name].iter_rows(values_only=True)]
    except SpreadsheetError:
        raise
    except Exception as exc:
        raise SpreadsheetError(
            f"openpyxl could not read {os.path.basename(path)}: {exc}"
        ) from exc
    finally:
        workbook.close()

    # iter_rows already pads to the sheet's declared width, but a ragged final
    # row is possible; make the result rectangular either way.
    width = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (width - len(r)) for r in rows]
    while rows and not any(cell for cell in rows[-1]):
        rows.pop()
    return rows


def _openpyxl_cell_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return _format_datetime(value)
    if isinstance(value, (int, float)):
        return _number_str(value)
    return str(value)


# --------------------------------------------------------------------------- #
# Delimited text and .xls                                                      #
# --------------------------------------------------------------------------- #

def _read_delimited(path):
    delimiter = "\t" if path.lower().endswith((".tsv", ".tab")) else None
    with open(path, newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","
        rows = [[cell.strip() for cell in row]
                for row in csv.reader(handle, delimiter=delimiter)]

    width = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (width - len(r)) for r in rows]
    while rows and not any(cell for cell in rows[-1]):
        rows.pop()
    return rows


def _read_xls(path, sheet):
    """Read a legacy .xls file.  Needs xlrd, which is genuinely optional."""
    try:
        import xlrd
    except ImportError:
        raise SpreadsheetError(
            f"{os.path.basename(path)} is a legacy .xls file, which needs the "
            "xlrd package. Either install xlrd, or open the file and save it "
            "as .xlsx or .csv -- both are read with no extra packages."
        )

    book = xlrd.open_workbook(path)
    names = book.sheet_names()
    name, _ = _select_sheet([(n, n) for n in names], sheet, path)
    worksheet = book.sheet_by_name(name)

    rows = []
    for r in range(worksheet.nrows):
        row = []
        for c in range(worksheet.ncols):
            cell = worksheet.cell(r, c)
            if cell.ctype == xlrd.XL_CELL_DATE:
                row.append(_format_datetime(
                    datetime.datetime(*xlrd.xldate_as_tuple(cell.value, book.datemode))
                ))
            elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                row.append("TRUE" if cell.value else "FALSE")
            elif cell.ctype == xlrd.XL_CELL_NUMBER:
                row.append(_number_str(cell.value))
            elif cell.ctype == xlrd.XL_CELL_EMPTY:
                row.append("")
            else:
                row.append(str(cell.value))
        rows.append(row)
    while rows and not any(cell for cell in rows[-1]):
        rows.pop()
    return rows


# --------------------------------------------------------------------------- #
# Public interface                                                             #
# --------------------------------------------------------------------------- #

def _select_sheet(sheets, sheet, path):
    """Resolve a sheet selector against [(name, handle)] in workbook order.

    ``sheet`` is a name, a 0-based index, or None for the first sheet.  A name
    that misses is retried case-insensitively before giving up, and the error
    lists what the workbook actually contains.
    """
    if sheet is None:
        return sheets[0]
    if isinstance(sheet, int) and not isinstance(sheet, bool):
        try:
            return sheets[sheet]
        except IndexError:
            raise SpreadsheetError(
                f"{os.path.basename(path)} has {len(sheets)} sheet(s); "
                f"index {sheet} is out of range"
            )
    for name, handle in sheets:
        if name == sheet:
            return name, handle
    for name, handle in sheets:
        if name.strip().lower() == str(sheet).strip().lower():
            return name, handle
    raise SpreadsheetError(
        f"{os.path.basename(path)} has no sheet named {sheet!r} "
        f"(found: {', '.join(repr(n) for n, _ in sheets)})"
    )


def sheet_names(path):
    """Return the worksheet names of a workbook, in workbook order.

    Delimited text has no sheets, so it reports a single sheet named after the
    file -- callers can treat every format the same way.
    """
    path = str(path)
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".xlsx":
        with zipfile.ZipFile(path) as archive:
            return [name for name, _ in _xlsx_sheets(archive)]
    if suffix == ".xls":
        try:
            import xlrd
        except ImportError:
            raise SpreadsheetError(
                f"{os.path.basename(path)} is a legacy .xls file, which needs "
                "the xlrd package."
            )
        return list(xlrd.open_workbook(path, on_demand=True).sheet_names())
    return [os.path.basename(path)]


def read_sheet(path, sheet=None, backend="auto"):
    """Read one worksheet and return it as a rectangular list of string rows.

    ``path``    .xlsx, .csv, .tsv or .xls (the last needs xlrd).
    ``sheet``   sheet name, 0-based index, or None for the first sheet.
                Ignored for delimited text, which has only one.
    ``backend`` ``"auto"`` (default), ``"stdlib"`` or ``"openpyxl"``.

    ``auto`` reads .xlsx with the stdlib parser and only reaches for openpyxl if
    that fails, so the common path behaves identically whether or not openpyxl
    happens to be installed in the environment that ran the pipeline -- while an
    unusual file can still be rescued on a machine that has it.

    Row *n* of the file is index ``n - 1`` of the result.  Every cell is a str;
    empty cells are ``""``.  Trailing blank rows and columns are trimmed.
    """
    path = str(path)
    if not os.path.isfile(path):
        raise SpreadsheetError(f"spreadsheet not found: {path}")
    if backend not in ("auto", "stdlib", "openpyxl"):
        raise SpreadsheetError(
            f"unknown backend {backend!r} (expected auto, stdlib or openpyxl)"
        )

    suffix = os.path.splitext(path)[1].lower()

    if suffix in (".csv", ".tsv", ".tab", ".txt"):
        return _read_delimited(path)
    if suffix == ".xls":
        return _read_xls(path, sheet)
    if suffix != ".xlsx":
        raise SpreadsheetError(
            f"unsupported spreadsheet format {suffix!r} for {os.path.basename(path)} "
            "(expected .xlsx, .xls, .csv or .tsv)"
        )

    if backend == "openpyxl":
        return _read_xlsx_openpyxl(path, sheet)

    try:
        return _read_xlsx_stdlib(path, sheet)
    except (SpreadsheetError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        if backend == "stdlib":
            raise SpreadsheetError(
                f"could not read {os.path.basename(path)}: {exc}"
            ) from exc
        try:
            return _read_xlsx_openpyxl(path, sheet)
        except Exception:
            # The fallback failed too -- report the original stdlib failure,
            # which describes the file rather than the rescue attempt.
            raise SpreadsheetError(
                f"could not read {os.path.basename(path)}: {exc}"
            ) from exc
