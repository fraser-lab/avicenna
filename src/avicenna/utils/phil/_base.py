"""PhilBase: abstract base class for all Avicenna PHIL parameter objects."""

import re
import freephil


def _simplify_choices(s: str) -> str:
    """Collapse freephil choice params from 'word *selected word' to 'selected'."""
    s = re.sub(r'[ \t]*\\\n[ \t]*', ' ', s)  # join explicit continuation lines first
    # Use [ \t] (not \s) so the pattern never crosses a newline into the next param
    return re.sub(r'(=[ \t]*)(?:\w+[ \t]+)*\*(\w+)(?:[ \t]+\w+)*', r'\1\2', s)


def _choice_names_from_def(phil_def: str) -> frozenset:
    """Scan a PHIL definition string and return names of all choice-type params.

    freephil validates choice values case-sensitively, so values like 'P1'
    would silently fail validation and revert to the previous setting.
    We collect these names once so adopt_phil_string can normalize them first.
    """
    names = set()
    lines = phil_def.splitlines()
    for i, line in enumerate(lines):
        if re.search(r'\.type\s*=\s*choice', line):
            for j in range(i - 1, max(i - 6, -1), -1):
                m = re.match(r'[ \t]*(\w+)\s*=', lines[j])
                if m:
                    names.add(m.group(1))
                    break
    return frozenset(names)


def _normalize_choice_case(text: str, choice_names: frozenset) -> str:
    """Lowercase values of choice-type params in a raw PHIL string.

    Only unquoted single-word values are affected; everything else is left alone.
    """
    if not choice_names:
        return text
    pattern = (
        r'(?m)^([ \t]*)('
        + '|'.join(re.escape(n) for n in sorted(choice_names, key=len, reverse=True))
        + r')([ \t]*=[ \t]*)([^\s"\'#][^\s#]*|[^\s"\'#])'
    )
    return re.sub(pattern, lambda m: m.group(1) + m.group(2) + m.group(3) + m.group(4).lower(), text)


class PhilBase:
    """Abstract base for all Avicenna PHIL parameter objects.

    Subclasses set ``phil_def`` (a PHIL definition string) at the class level.
    Instances hold a ``params`` scope_extract built from that definition.
    """

    phil_def: str = ""

    def __init__(self):
        self._master = freephil.parse(self.phil_def)
        self._params = self._master.fetch().extract()
        self._choice_names = _choice_names_from_def(self.phil_def)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def master(self):
        """The freephil master scope (definition object)."""
        return self._master

    @property
    def params(self):
        """The adopted scope_extract (parameter values)."""
        return self._params

    # ------------------------------------------------------------------
    # Adopt
    # ------------------------------------------------------------------

    def adopt_phil_string(self, phil_string: str):
        """Merge a PHIL-formatted string into the current params."""
        phil_string = _normalize_choice_case(phil_string, self._choice_names)
        user_phil = freephil.parse(phil_string)
        current_phil = freephil.parse(self.as_phil_string())
        self._params = self._master.fetch(sources=[current_phil, user_phil]).extract()

    def from_file(self, path: str):
        """Read a .phil file and merge it into the current params."""
        with open(path) as fh:
            self.adopt_phil_string(fh.read())

    def from_dict(self, d: dict, scope: str = ""):
        """Adopt a flat key->value dict as PHIL overrides.

        *scope* is prepended to each key so callers can target a nested scope,
        e.g. ``scope="processing.xds"`` turns ``{"ncpu": 16}`` into the
        PHIL line ``processing.xds.ncpu = 16``.
        """
        prefix = f"{scope}." if scope else ""
        lines = []
        for k, v in d.items():
            if v is None:
                continue
            if isinstance(v, bool):
                lines.append(f"{prefix}{k} = {str(v).upper()}")
            elif isinstance(v, str):
                lines.append(f'{prefix}{k} = "{v}"')
            else:
                lines.append(f"{prefix}{k} = {v}")
        if lines:
            self.adopt_phil_string("\n".join(lines))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def to_file(self, path: str):
        """Write the full (all-params) PHIL to *path*."""
        with open(path, "w") as fh:
            fh.write(self.as_phil_string())

    def as_phil_string(self) -> str:
        """Return the current params as a PHIL-formatted string."""
        return self._master.format(self._params).as_str()

    def full_phil_string(self) -> str:
        """Return all parameters as a compact PHIL string matching default_*.phil style.

        Within ``step {}`` blocks only the sub-scope matching ``step.program``
        is emitted; step-level None params are omitted; choice params are
        rendered as single selected values rather than freephil's
        ``*selected other1 other2`` notation."""
        from avicenna.utils.phil._serialize import compact_phil_string
        return compact_phil_string(self)

    def diff_phil_string(self) -> str:
        """Return only parameters that differ from their defaults.

        Choice parameters are rendered as single selected values rather than
        the full ``*selected other1 other2`` freephil notation."""
        diff = self._master.fetch_diff(
            sources=[freephil.parse(self.as_phil_string())],
        )
        return _simplify_choices(diff.as_str())

    def show(self, attributes_level: int = 0):
        """Print the current params (attributes_level controls annotation verbosity)."""
        self._master.format(self._params).show(attributes_level=attributes_level)
