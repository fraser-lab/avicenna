"""Every byte of code in this repository must be ASCII.

The one thing that is exempt is text the program deliberately emits: the
spinner, the progress bar, box-drawing rules, unit labels such as the Angstrom
sign.  Those glyphs are the output, and replacing them would change what the
user sees rather than clean anything up.  So the rule this enforces is:

    string literals may hold anything;
    comments, docstrings and code itself must be ASCII.

Which is checkable without an allowlist that goes stale.
"""

import ast
import io
import pathlib
import tokenize

ROOT = pathlib.Path(__file__).resolve().parent.parent

CODE_SUFFIXES = {".py", ".sh", ".phil", ".toml", ".cfg"}
SKIP_DIRS = {".git", "__pycache__", "build", "dist", ".pixi", ".eggs"}

# Vendored third-party source, kept byte-for-byte.  Its non-ASCII characters are
# in the revision-history comments naming the people who wrote it; mangling
# someone's name to satisfy a local convention is not a fix.  Exempt as a whole
# file so the exemption cannot quietly widen.
EXEMPT = {"src/avicenna/resources/scripts/generate_XDS.INP"}


# Token types whose text is a string literal.  From Python 3.12 an f-string is
# no longer one STRING token but a START/MIDDLE/END trio, with the expressions
# inside it tokenised normally -- which is what we want, since only the literal
# text is output and the expressions are code.
_STRING_TOKENS = frozenset(
    value
    for value in (
        getattr(tokenize, name, None)
        for name in ("STRING", "FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
    )
    if value is not None
)


def _code_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix not in CODE_SUFFIXES:
            continue
        if str(path.relative_to(ROOT)) in EXEMPT:
            continue
        yield path


def _offenders(text, line_offset=0):
    """Return ``[(line, column, character)]`` for every non-ASCII character."""
    found = []
    for row, line in enumerate(text.splitlines(), start=1):
        for column, char in enumerate(line, start=1):
            if ord(char) > 127:
                found.append((row + line_offset, column, char))
    return found


def _describe(path, offenders):
    where = path.relative_to(ROOT)
    return "\n".join(
        f"{where}:{line}:{column}: U+{ord(char):04X} {char!r}"
        for line, column, char in offenders
    )


def test_python_code_and_comments_are_ascii():
    """Everything but a string literal must be ASCII in a Python file."""
    problems = []
    for path in _code_files():
        if path.suffix != ".py":
            continue
        source = path.read_text(encoding="utf-8")
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        offenders = []
        for token in tokens:
            if token.type in _STRING_TOKENS:
                continue  # deliberate output text is allowed
            if token.string.isascii():
                continue
            row, column = token.start
            for index, char in enumerate(token.string):
                if ord(char) > 127:
                    offenders.append((row, column + index + 1, char))
        if offenders:
            problems.append(_describe(path, offenders))
    assert not problems, "Non-ASCII characters outside string literals:\n" + "\n".join(
        problems
    )


def test_docstrings_are_ascii():
    """A docstring is documentation, not output, so it follows the code rule."""
    problems = []
    for path in _code_files():
        if path.suffix != ".py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        nodes = [tree] + [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        offenders = []
        for node in nodes:
            text = ast.get_docstring(node, clean=False)
            if text is None or text.isascii():
                continue
            # The docstring's own first line, so the report points into the file.
            start = node.body[0].lineno if node.body else 1
            offenders.extend(_offenders(text, line_offset=start - 1))
        if offenders:
            problems.append(_describe(path, sorted(offenders)))
    assert not problems, "Non-ASCII characters in docstrings:\n" + "\n".join(problems)


def test_comments_in_non_python_files_are_ascii():
    """Shell, PHIL and config comments follow the same rule as Python ones."""
    problems = []
    for path in _code_files():
        if path.suffix == ".py":
            continue
        offenders = []
        for row, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.lstrip().startswith("#"):
                continue
            for column, char in enumerate(line, start=1):
                if ord(char) > 127:
                    offenders.append((row, column, char))
        if offenders:
            problems.append(_describe(path, offenders))
    assert not problems, "Non-ASCII characters in comments:\n" + "\n".join(problems)


def test_every_code_file_decodes_as_ascii_or_utf8():
    """A file that is neither is unreadable to half the tools that will open it."""
    problems = []
    for path in _code_files():
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            problems.append(f"{path.relative_to(ROOT)}: {exc}")
    assert not problems, "Files that are not valid UTF-8:\n" + "\n".join(problems)


def test_the_exemption_list_still_applies():
    """An exempt file that has gone away should drop off the list, not linger."""
    missing = [name for name in EXEMPT if not (ROOT / name).is_file()]
    assert not missing, (
        "EXEMPT names files that no longer exist; remove them: " + ", ".join(missing)
    )
