"""PhilBase: abstract base class for all Avicenna PHIL parameter objects."""

import freephil


class PhilBase:
    """Abstract base for all Avicenna PHIL parameter objects.

    Subclasses set ``phil_def`` (a PHIL definition string) at the class level.
    Instances hold a ``params`` scope_extract built from that definition.
    """

    phil_def: str = ""

    def __init__(self):
        self._master = freephil.parse(self.phil_def)
        self._params = self._master.fetch().extract()

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
        user_phil = freephil.parse(phil_string)
        self._params = self._master.fetch(sources=[user_phil]).extract()

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

    def diff_phil_string(self) -> str:
        """Return only parameters that differ from their defaults."""
        diff = self._master.fetch_diff(
            sources=[freephil.parse(self.as_phil_string())],
        )
        return diff.as_str()

    def show(self, attributes_level: int = 0):
        """Print the current params (attributes_level controls annotation verbosity)."""
        self._master.format(self._params).show(attributes_level=attributes_level)
