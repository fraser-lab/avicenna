"""PhilBase: abstract base class for all Avicenna PHIL parameter objects."""

import re
import sys
from pathlib import Path

import freephil


def _simplify_choices(s: str) -> str:
    """Collapse freephil choice notation in a PHIL string.

    A single selection ('a *b c') becomes 'b' (bare -- re-parses for single- and
    multi-choice alike).  Multiple selections ('*a b *c') become '*a *c': the
    stars are REQUIRED for a multi-choice to re-parse to more than one value
    (bare space-separated values parse to an empty selection).  A value run with
    no '*' is left untouched.  Dotted option names (e.g. phenix.phaser) are
    handled.
    """
    s = re.sub(r'[ \t]*\\\n[ \t]*', ' ', s)  # join explicit continuation lines first

    def _repl(m):
        prefix, value = m.group(1), m.group(2)
        starred = [t[1:] for t in value.split() if t.startswith('*')]
        if not starred:
            return m.group(0)                       # no selection marker -> as-is
        if len(starred) == 1:
            return prefix + starred[0]              # single -> bare value
        return prefix + ' '.join('*' + v for v in starred)  # multi -> keep stars

    # Use [ \t] (not \s) so a value run never crosses a newline into the next param.
    return re.sub(r'(=[ \t]*)((?:\*?[\w.]+[ \t]*)+)', _repl, s)


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


def _source_step_program(step_obj):
    """Value of the ``program`` definition inside a parsed step scope, or None.

    Handles choice rendering (``program = *selected other``): the starred token
    is the selected value.  A plain assignment returns its single value.
    """
    for o in step_obj.objects:
        if o.name == "program" and not hasattr(o, "objects") and o.words:
            for w in o.words:
                if w.value.startswith("*"):
                    return w.value[1:]
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


def _coalesce_duplicate_scopes(scope):
    """Merge sibling sub-scopes that share a name into a single instance.

    A command line cannot express two distinct ``.multiple`` instances (there is
    no way to delimit them), yet freephil's command-line interpreter emits one
    instance per leaf assignment -- so ``space_group=.. unit_cell=..`` both under
    ``known_symmetry.isoform`` produce two half-filled isoforms.  This collapses
    same-named sibling scopes back into one, recursively, merging their children
    via ``_source_overlay``.  Purely structural (matches by object ``.name``);
    intended for the CLI-derived phil only, never for user phil files (which can
    legitimately carry multiple instances).  Mutates *scope* in place."""
    seen = {}
    kept = []
    for o in scope.objects:
        if hasattr(o, "objects"):                 # sub-scope
            if o.name in seen:
                _source_overlay(seen[o.name], o)
            else:
                seen[o.name] = o
                kept.append(o)
        else:                                     # definition
            kept.append(o)
    scope.objects = kept
    for o in kept:
        if hasattr(o, "objects"):
            _coalesce_duplicate_scopes(o)


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
            if sid is not None:
                match = by_id.get(sid)
            else:
                # A CLI override (e.g. ``pandda.analyse.cpus=32``) arrives as a
                # step with no id.  Match it to the unique existing step whose
                # program owns the block it carries, so the override lands on the
                # real step instead of appending an orphan.  Ambiguous (a program
                # used by several steps) or unmatched -> warn loudly and leave it
                # unmatched; the user must name the step by id.  freephil's own
                # .multiple merge does this inconsistently (works when several
                # steps exist, not for a lone step).
                candidates = self._candidate_steps_for(cur_steps, us)
                if len(candidates) == 1:
                    match = candidates[0]
                else:
                    match = None
                    self._warn_unmatched_step(us, candidates)
            if match is not None:
                _source_overlay(match, us)
            else:
                cur_steps.objects.append(us)
                by_id[sid] = us
        usr_steps.objects = [o for o in usr_steps.objects if o.name != "step"]

    def _candidate_steps_for(self, cur_steps, user_step):
        """Current steps whose selected program owns a block present in the
        id-less *user_step*.  Empty if the step carries no program block."""
        user_blocks = {o.name for o in user_step.objects if hasattr(o, "objects")}
        if not user_blocks:
            return []
        candidates = []
        for o in cur_steps.objects:
            if o.name != "step":
                continue
            prog = _source_step_program(o)
            block = self._program_blocks.get(prog, prog)
            if block in user_blocks:
                candidates.append(o)
        return candidates

    def _warn_unmatched_step(self, user_step, candidates):
        """Print a prominent stderr warning when an id-less step override could
        not be applied -- so an eaten parameter is never silent."""
        blocks = sorted(o.name for o in user_step.objects if hasattr(o, "objects"))
        if not blocks:
            return  # nothing block-like to attribute the override to
        block = ", ".join(blocks)
        if candidates:
            ids = ", ".join(str(_source_step_id(c)) for c in candidates)
            why = (f"its program runs in {len(candidates)} steps ({ids}), so the "
                   f"target is ambiguous. Name the step by id, e.g. add "
                   f"`steps.step.id=<one of: {ids}>` to the override")
        else:
            why = (f"no step in this pipeline runs the '{block}' program "
                   f"(check the program name and that a step uses it)")
        bar = "!" * 72
        print(f"\n{bar}\n"
              f"WARNING: PHIL override for '{block}' was IGNORED (not applied to any step).\n"
              f"  {why}.\n"
              f"{bar}\n",
              file=sys.stderr, flush=True)

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
        self._adopt_parsed(freephil.parse(phil_string))

    def _adopt_parsed(self, user_phil):
        """Merge an already-parsed user phil scope into the current params.

        Shared by ``adopt_phil_string`` and ``adopt_cli_args``.  Taking a parsed
        scope (rather than re-stringifying) matters for the CLI path: a coalesced
        ``.multiple`` instance, if round-tripped through ``as_str()``, would be
        re-emitted as separate dotted leaf lines and split apart again on reparse.
        """
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
            # The interpreter emits one object per arg, so several leaf
            # assignments into the same .multiple scope arrive as separate
            # instances.  Coalesce them into one before adopting (a command line
            # cannot mean two distinct instances), then adopt the parsed scope
            # directly -- round-tripping through as_str() would re-split it.
            # See _coalesce_duplicate_scopes / _adopt_parsed.
            joined = _normalize_choice_case(
                "\n".join(w.as_str() for w in working), self._choice_names)
            combined = freephil.parse(joined)
            _coalesce_duplicate_scopes(combined)
            self._adopt_parsed(combined)
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

    def default_diff_phil_string(self) -> str:
        """Return only parameters that differ from the shipped DEFAULT PHIL.

        Unlike ``diff_phil_string`` -- which diffs against the bare master
        schema defaults and therefore always re-emits the full shipped step
        list -- this diffs against the ``default_<name>.phil`` resource that
        ships the pipeline defaults.  A run that uses only defaults yields an
        empty string; only the user's actual overrides appear.  Intended for
        logging "what is non-default about this run" at the top of a run log.

        Choice parameters are rendered as single selected values rather than
        the full ``*selected other1 other2`` freephil notation."""
        default_master = self._master.fetch(sources=self._default_sources)
        diff = default_master.fetch_diff(
            sources=[freephil.parse(self.as_phil_string())],
        )
        return _simplify_choices(diff.as_str())

    def show(self, attributes_level: int = 0):
        """Print the current params (attributes_level controls annotation verbosity)."""
        self._master.format(self._params).show(attributes_level=attributes_level)
