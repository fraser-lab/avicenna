"""PhilBase: abstract base class for all Avicenna PHIL parameter objects."""

import re
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Source-level helpers for field-merging .multiple step overrides
#
# freephil cannot field-merge .multiple scope instances (it appends them), and
# at the extract level we cannot tell a user-set value from a master default.
# So we merge at the parsed-source level, where only the fields the user wrote
# are present.  All helpers are structural (match by object .name); they have
# no knowledge of specific step ids, program names, or field names.
# ---------------------------------------------------------------------------

def _source_child_scope(scope, name):
    """First child of *scope* that is a sub-scope named *name*, or None."""
    for o in scope.objects:
        if o.name == name and hasattr(o, "objects"):
            return o
    return None


def _source_step_id(step_obj):
    """Value of the ``id`` definition inside a parsed step scope, or None."""
    for o in step_obj.objects:
        if o.name == "id" and not hasattr(o, "objects") and o.words:
            return o.words[0].value
    return None


def _source_overlay(dst, src):
    """Overlay parsed scope *src*'s children onto *dst* by name, recursively.

    Matching sub-scopes are merged recursively; matching definitions are
    replaced with the src value; names present only in src are appended.
    Mutates *dst* in place.  Purely structural -- no pipeline-specific names.
    """
    index = {}
    for o in dst.objects:
        index.setdefault(o.name, o)
    for so in src.objects:
        existing = index.get(so.name)
        if hasattr(so, "objects"):                       # sub-scope
            if existing is not None and hasattr(existing, "objects"):
                _source_overlay(existing, so)
            else:
                dst.objects.append(so)
                index[so.name] = so
        else:                                            # definition
            if existing is not None and not hasattr(existing, "objects"):
                dst.objects[dst.objects.index(existing)] = so
            else:
                dst.objects.append(so)
            index[so.name] = so


class PhilBase:
    """Abstract base for all Avicenna PHIL parameter objects.

    A steps-based sub-pipeline subclass only needs three class attributes:

        phil_def       -- the PHIL definition string (built from program blocks)
        _resource_file -- default_<name>.phil under resources/pipelines (defaults)
        _scope_name    -- top-level sub-pipeline scope, e.g. "refine"

    The base then handles default loading, choice-case normalization, accumulating
    merges, and field-merging ``.multiple`` step overrides by id.  Subclasses that
    do not follow the steps model (e.g. AvicennaPHIL) override ``__init__`` and
    ``adopt_phil_string`` directly.
    """

    phil_def: str = ""
    _resource_file = None   # default_<name>.phil under resources/pipelines
    _scope_name = None      # top-level sub-pipeline scope (e.g. "refine")
    _program_blocks = {}    # {program choice value: settings-block scope name}
    _PIPELINES_DIR = Path(__file__).parents[2] / "resources" / "pipelines"

    def __init__(self, load_defaults: bool = True):
        self._master = freephil.parse(self.phil_def)
        self._choice_names = _choice_names_from_def(self.phil_def)
        self._default_sources = []
        if load_defaults and self._resource_file:
            resource = self._PIPELINES_DIR / self._resource_file
            if resource.exists():
                self._default_sources = [freephil.parse(resource.read_text())]
        self._params = self._master.fetch(sources=self._default_sources).extract()

    # ------------------------------------------------------------------
    # Step override field-merge (no-op when _scope_name is unset)
    # ------------------------------------------------------------------

    def _steps_scope_obj(self, phil):
        """The parsed ``<scope>.steps`` source scope within *phil*, or None."""
        if not self._scope_name:
            return None
        scope = _source_child_scope(phil, self._scope_name)
        return _source_child_scope(scope, "steps") if scope is not None else None

    def _merge_user_steps(self, current_phil, user_phil):
        """Field-merge user step overrides onto matching current steps, by id.

        A step present in both is merged field-by-field (user value wins per
        field, shipped values kept otherwise); a step whose id is new is
        appended, preserving order.  The user phil's step objects are then
        removed so the subsequent fetch contributes only its non-step overrides
        (which freephil field-merges natively).  Mutates both phils in place.

        Doing this at the source level (rather than letting freephil append the
        ``.multiple`` instances and deduping the extract) means a partial step
        override keeps the rest of that step's shipped settings -- "specify only
        what you change" -- and it also sidesteps freephil's reordering of
        appended ``.multiple`` instances, since only one source carries steps."""
        cur_steps = self._steps_scope_obj(current_phil)
        usr_steps = self._steps_scope_obj(user_phil)
        if cur_steps is None or usr_steps is None:
            return
        by_id = {}
        for o in cur_steps.objects:
            if o.name == "step":
                by_id[_source_step_id(o)] = o
        for us in [o for o in usr_steps.objects if o.name == "step"]:
            sid = _source_step_id(us)
            match = by_id.get(sid)
            if match is not None:
                _source_overlay(match, us)
            else:
                cur_steps.objects.append(us)
                by_id[sid] = us
        usr_steps.objects = [o for o in usr_steps.objects if o.name != "step"]

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
        """Merge a PHIL-formatted string into the current params.

        Choice values are case-normalized first (freephil validates choices
        case-sensitively).  The merge rebases on the current params so repeated
        calls accumulate.  Step overrides are field-merged onto the matching
        step by id (see ``_merge_user_steps``), so a partial step override keeps
        that step's other shipped settings.
        """
        phil_string = _normalize_choice_case(phil_string, self._choice_names)
        user_phil = freephil.parse(phil_string)
        current_phil = freephil.parse(self.as_phil_string())
        self._merge_user_steps(current_phil, user_phil)
        self._params = self._master.fetch(sources=[current_phil, user_phil]).extract()

    def from_file(self, path: str):
        """Read a .phil file and merge it into the current params."""
        with open(path) as fh:
            self.adopt_phil_string(fh.read())

    def adopt_cli_args(self, args):
        """Interpret leftover command-line tokens as PHIL and merge them.

        Accepts full or partial parameter paths (``key=value``) and ``.phil``
        file paths, via freephil's command-line interpreter -- it auto-resolves
        unambiguous partial names, errors with a "Best matches" list on
        ambiguity, and errors on unknown parameters (so typos are caught).  The
        interpreted definitions are routed through ``adopt_phil_string`` so
        choice-case normalization and step field-merge still apply.

        Returns the list of tokens consumed (empty if none).  Raises on bad
        input; callers typically convert to a clean CLI error.
        """
        args = [a for a in (args or []) if a]
        if not args:
            return []
        interp = self._master.command_line_argument_interpreter(
            home_scope=self._scope_name)
        try:
            working = interp.process_args(args)
        except freephil.Sorry as e:
            # freephil.Sorry is not an Exception subclass; re-raise as ValueError
            # so CLI callers can catch it with a normal handler.
            raise ValueError(str(e)) from None
        if working:
            self.adopt_phil_string("\n".join(w.as_str() for w in working))
        return args

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

    def full_phil_string(self, expert_level=None, attributes: bool = False) -> str:
        """Return parameters as a compact PHIL string matching default_*.phil style.

        This is the single renderer for every output path (dry-run/-c/-e/-a).
        Within ``step {}`` blocks only the sub-scope matching ``step.program`` is
        emitted; step-level None params are omitted; choice params are rendered
        as single selected values rather than freephil's ``*selected other``
        notation.  ``expert_level`` and ``attributes`` mirror freephil's
        ``show(expert_level=..., attributes_level=...)``."""
        from avicenna.utils.phil._serialize import compact_phil_string
        return compact_phil_string(self, expert_level=expert_level, attributes=attributes)

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
