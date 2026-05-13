"""Compact PHIL serializer for Avicenna pipeline parameter objects.

Produces output that matches the style of the shipped default_*.phil files:
  - Within ``step {}`` blocks (multiple scopes that have a ``program`` child
    parameter), only the sub-scope whose name equals ``step.program`` is
    emitted; all other program sub-scopes are dropped.
  - Step-level scalar params whose value is None are omitted, except for
    ``id`` and ``program`` which are always shown.
  - All params inside a program sub-scope are shown, including None values.
  - Choice params are rendered as the selected value only (no ``*`` notation).
"""

import re

# Matches simple PHIL identifiers that don't need quoting.
_SIMPLE = re.compile(r'^[a-zA-Z0-9_-]+$')
# Reserved words that must be quoted when they appear as string values.
_RESERVED = {'None', 'True', 'False'}
# Strips freephil choice notation: '*selected other1 other2' → 'selected'
_CHOICE_RE = re.compile(r'(=[ \t]*)(?:\w+[ \t]+)*\*(\w+)(?:[ \t]+\w+)*')


def _val_str(v) -> str:
    """Format a scalar value for PHIL output."""
    if v is None:
        return "None"
    if isinstance(v, bool):           # must precede int check (bool is int)
        return str(v)
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return " ".join(_val_str(x) for x in v)
    s = str(v)
    if _SIMPLE.match(s) and s not in _RESERVED:
        return s
    return f'"{s}"'


def _is_step_scope(obj) -> bool:
    """True when *obj* is a multiple scope containing a 'program' child param."""
    if not getattr(obj, 'multiple', False):
        return False
    return any(
        not hasattr(c, 'objects') and c.name == 'program'
        for c in obj.objects
    )


def _format_scope(master_scope, extract, indent="") -> list[str]:
    """Recursively format a scope, returning lines without a trailing newline."""
    lines = []
    ind2 = indent + "  "
    for obj in master_scope.objects:
        name = obj.name
        val = getattr(extract, name, None)
        if hasattr(obj, 'objects'):
            if getattr(obj, 'multiple', False):
                items = list(val) if val is not None else []
                fmt = _format_step if _is_step_scope(obj) else _format_scope
                for item in items:
                    sub = fmt(obj, item, ind2)
                    lines.append(f"{indent}{name} {{")
                    lines.extend(sub)
                    lines.append(f"{indent}}}")
            else:
                sub = _format_scope(obj, val, ind2) if val is not None else []
                lines.append(f"{indent}{name} {{")
                lines.extend(sub)
                lines.append(f"{indent}}}")
        else:
            lines.append(f"{indent}{name} = {_val_str(val)}")
    return lines


def _format_step(master_scope, step_extract, indent="") -> list[str]:
    """Format a step block, emitting only the sub-scope matching step.program."""
    lines = []
    ind2 = indent + "  "
    program = getattr(step_extract, 'program', None)
    always_show = {'id', 'program'}

    for obj in master_scope.objects:
        name = obj.name
        val = getattr(step_extract, name, None)
        if hasattr(obj, 'objects'):
            # Program sub-scope: only emit the one that matches step.program.
            if name == program:
                sub = _format_scope(obj, val, ind2)
                if sub:
                    lines.append(f"{indent}{name} {{")
                    lines.extend(sub)
                    lines.append(f"{indent}}}")
        else:
            if val is not None or name in always_show:
                lines.append(f"{indent}{name} = {_val_str(val)}")
    return lines


def compact_phil_string(phil_base) -> str:
    """Serialize a PhilBase instance to compact PHIL matching default_*.phil style.

    Parameters
    ----------
    phil_base : PhilBase
        Any avicenna PHIL parameter object (ProcessingPHIL, RefinePHIL, …).

    Returns
    -------
    str
        A PHIL-formatted string suitable for writing to a *.phil file and
        re-loading with ``phil_base.from_file()``.
    """
    lines = _format_scope(phil_base._master, phil_base._params)
    text = "\n".join(lines) + "\n"
    return _CHOICE_RE.sub(r'\1\2', text)
