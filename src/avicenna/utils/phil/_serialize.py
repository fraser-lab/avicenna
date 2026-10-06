"""Compact PHIL serializer for Avicenna pipeline parameter objects.

This is the single renderer for every Avicenna PHIL output path (dry-run,
``-c``, ``-e LEVEL``, ``-a``).  It produces output that matches the style of the
shipped default_*.phil files:
  - Within ``step {}`` blocks (multiple scopes that have a ``program`` child
    parameter), only the sub-scope whose name equals ``step.program`` is
    emitted; all other program sub-scopes are dropped.  This is the key thing
    freephil's native ``show()`` cannot do (it walks the static master and
    prints every defined child for every step instance).
  - Step-level scalar params whose value is None are omitted, except for
    ``id`` and ``program`` which are always shown.
  - All params inside a program sub-scope are shown, including None values.
  - Choice params are rendered as the selected value only (no ``*`` notation).

Two optional knobs reproduce what freephil ``show()`` offered:
  - ``expert_level``: params/sub-scopes whose expert_level exceeds the requested
    level are omitted (None shows everything).
  - ``attributes``: emit ``.short_caption``/``.help``/``.type``/``.expert_level``
    annotation lines (valid PHIL, so the output still round-trips).
"""

import re

import freephil

# Matches simple PHIL identifiers that don't need quoting.
_SIMPLE = re.compile(r'^[a-zA-Z0-9_-]+$')
# Reserved words that must be quoted when they appear as string values.
_RESERVED = {'None', 'True', 'False'}


def _val_str(v) -> str:
    """Format a scalar value for PHIL output."""
    if v is None:
        return "None"
    # freephil's Auto sentinel str()s to "Auto"; emit lowercase "auto" so it
    # reloads as the same sentinel (idempotent round-trip) and reads as the
    # choice value users write.  A choice loaded from a full "*auto ..." spec is
    # the string "auto" and renders the same way, so both forms converge.
    if v is freephil.Auto:
        return "auto"
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


def _render_value(obj, val) -> str:
    """Format a leaf value, rendering a multi-choice as its full option menu.

    A multi-choice (``.type = choice(multi=True)``) is emitted as every option
    with ``*`` on the selected ones (e.g. ``DSI_1 DSI_2 *EE_1 all``).  This keeps
    the menu discoverable AND re-parses correctly -- a bare space-separated list
    of values parses to an EMPTY selection in freephil.  Everything else falls
    through to ``_val_str``.
    """
    t = getattr(obj, 'type', None)
    if (t is not None and getattr(t, 'phil_type', None) == 'choice'
            and getattr(t, 'multi', False)):
        selected = set(val or [])
        options  = [w.value.lstrip('*') for w in (obj.words or [])]
        return ' '.join(f'*{o}' if o in selected else o for o in options)
    return _val_str(val)


def _quote(s) -> str:
    """Double-quote an attribute string value."""
    return '"' + str(s).replace('"', '\\"') + '"'


def _attr_lines(obj, indent) -> list[str]:
    """Return ``.attribute = value`` lines for a master definition object."""
    lines = []
    sc = getattr(obj, 'short_caption', None)
    if sc:
        lines.append(f"{indent}.short_caption = {_quote(sc)}")
    hp = getattr(obj, 'help', None)
    if hp:
        lines.append(f"{indent}.help = {_quote(hp)}")
    typ = getattr(obj, 'type', None)
    phil_type = getattr(typ, 'phil_type', None) if typ is not None else None
    if phil_type:
        lines.append(f"{indent}.type = {phil_type}")
    el = getattr(obj, 'expert_level', None)
    if el is not None:
        lines.append(f"{indent}.expert_level = {el}")
    return lines


def _open_scope(name, indent, obj, attributes) -> list[str]:
    """Lines that open a scope block (with attributes before the brace if asked)."""
    if attributes:
        lines = [f"{indent}{name}"]
        lines.extend(_attr_lines(obj, indent + "  "))
        lines.append(f"{indent}{{")
        return lines
    return [f"{indent}{name} {{"]


def _is_step_scope(obj) -> bool:
    """True when *obj* is a multiple scope containing a 'program' child param."""
    if not getattr(obj, 'multiple', False):
        return False
    return any(
        not hasattr(c, 'objects') and c.name == 'program'
        for c in obj.objects
    )


def _within_level(obj, expert_level) -> bool:
    """True if *obj* should be shown at the requested expert level.

    ``expert_level=None`` shows everything; otherwise objects whose own
    expert_level exceeds the requested level are dropped (None treated as 0).
    """
    if expert_level is None:
        return True
    return (getattr(obj, 'expert_level', None) or 0) <= expert_level


def _format_scope(master_scope, extract, indent="", expert_level=None,
                  attributes=False, program_blocks=None) -> list[str]:
    """Recursively format a scope, returning lines without a trailing newline."""
    lines = []
    ind2 = indent + "  "
    for obj in master_scope.objects:
        if not _within_level(obj, expert_level):
            continue
        name = obj.name
        val = getattr(extract, name, None)
        if hasattr(obj, 'objects'):
            if getattr(obj, 'multiple', False):
                items = list(val) if val is not None else []
                fmt = _format_step if _is_step_scope(obj) else _format_scope
                for item in items:
                    sub = fmt(obj, item, ind2, expert_level, attributes, program_blocks)
                    lines.extend(_open_scope(name, indent, obj, attributes))
                    lines.extend(sub)
                    lines.append(f"{indent}}}")
            else:
                sub = (_format_scope(obj, val, ind2, expert_level, attributes, program_blocks)
                       if val is not None else [])
                lines.extend(_open_scope(name, indent, obj, attributes))
                lines.extend(sub)
                lines.append(f"{indent}}}")
        else:
            lines.append(f"{indent}{name} = {_render_value(obj, val)}")
            if attributes:
                lines.extend(_attr_lines(obj, ind2))
    return lines


def _format_step(master_scope, step_extract, indent="", expert_level=None,
                 attributes=False, program_blocks=None) -> list[str]:
    """Format a step block, emitting only the sub-scope for step.program.

    The program choice value (e.g. ``phenix.refine``) need not equal its
    settings-block scope name (e.g. ``rigid_body``); ``program_blocks`` maps
    the former to the latter.  Falls back to identity when unmapped.
    """
    lines = []
    ind2 = indent + "  "
    program = getattr(step_extract, 'program', None)
    block_name = (program_blocks or {}).get(program, program)
    always_show = {'id', 'program'}

    for obj in master_scope.objects:
        name = obj.name
        # id/program are always shown regardless of expert level.
        if name not in always_show and not _within_level(obj, expert_level):
            continue
        val = getattr(step_extract, name, None)
        if hasattr(obj, 'objects'):
            # Program sub-scope: only emit the one belonging to step.program.
            if name == block_name:
                sub = _format_scope(obj, val, ind2, expert_level, attributes, program_blocks)
                lines.extend(_open_scope(name, indent, obj, attributes))
                lines.extend(sub)
                lines.append(f"{indent}}}")
        else:
            if val is not None or name in always_show:
                lines.append(f"{indent}{name} = {_render_value(obj, val)}")
                if attributes:
                    lines.extend(_attr_lines(obj, ind2))
    return lines


def compact_phil_string(phil_base, expert_level=None, attributes=False) -> str:
    """Serialize a PhilBase instance to compact PHIL matching default_*.phil style.

    Parameters
    ----------
    phil_base : PhilBase
        Any avicenna PHIL parameter object (ProcessingPHIL, RefinePHIL, ...).
    expert_level : int or None
        When given, parameters (and sub-scopes) whose expert_level exceeds this
        value are omitted, mirroring freephil ``show(expert_level=...)``.  None
        (the default) emits every parameter.
    attributes : bool
        When True, emit ``.short_caption``/``.help``/``.type``/``.expert_level``
        annotation lines, mirroring freephil ``show(attributes_level=...)``.

    Returns
    -------
    str
        A PHIL-formatted string suitable for writing to a *.phil file and
        re-loading with ``phil_base.from_file()``.
    """
    lines = _format_scope(phil_base._master, phil_base._params,
                          expert_level=expert_level, attributes=attributes,
                          program_blocks=getattr(phil_base, "_program_blocks", None))
    # Choice rendering (bare for single, full menu with stars for multi) is done
    # per-value in _render_value; no post-pass over the assembled text is needed.
    return "\n".join(lines) + "\n"
