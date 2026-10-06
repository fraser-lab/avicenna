"""Gathering the per-process records of a finished stage into info.json.

Every worker keeps its own record beside its own logs, and there are several
per dataset -- one per crystal form, index range and forced-integration retry.
When the stage ends, its sentinel reads them all, decides which one the dataset
should be represented by, and merges that into the project record.  Nothing is
re-derived from the output tree that a worker already knew.

The project record is the run's account of itself, so every dataset in it gets
an answer, including the ones that left no record at all.  Four states are told
apart:

  * a rejection marker and no working directory -- rejected before submission;
  * a working directory and no record -- the job died before its first step;
  * a record still saying ``running`` -- the job died, and the record names the
    step it was on;
  * a record with an outcome -- the job ran, well or badly.

A record that cannot be read is a fifth thing and not attributable to the
process: writes are atomic, so a killed worker leaves the previous complete
generation rather than a half-written file, and a write that failed leaves the
target untouched.  Damage after a complete write is damage to the file system,
so the dataset is written with its identity intact and this attempt is reported
as unprocessed.  Failing the whole run over it would punish every other dataset
in it, and such damage rarely arrives alone, so the count is reported too.

Selection is the job the result filter already does, so the records are
translated to its contract at one boundary (see ``avicenna.utils.terms``) and
handed to it.  The crystal's identity is the crystal code, taken from the
project record: a path would stop identifying the crystal the moment the run
directory moved.
"""

from collections import namedtuple
from pathlib import Path

from avicenna.utils.info import (
    CRYSTAL_LIST_FIELDS,
    FAILED_STAGE,
    OUTCOME_COMPLETED,
    OUTCOME_REJECTED,
    OUTCOME_RUNNING,
    OUTCOME_UNPROCESSED,
    PROCESS_PATTERN,
    STAGE_ANALYSIS,
    STAGE_PROCESS,
    STAGE_REFINE,
    STATUS_ANALYZED,
    STATUS_FAILED,
    STATUS_PROCESSED,
    STATUS_REFINED,
    ProcessInfo,
)
from avicenna.utils.terms import ProcessingTerms
from avicenna.utils.utils import ProcessingFilter

# What a gathered record must not supply.  The crystal-list fields are the
# crystal's identity, fixed when the project record was created; the dataset's
# own status and reason are decided here and set through set_status, which owns
# the pair.
_PROJECT_OWNED = set(CRYSTAL_LIST_FIELDS) | {"status", "failure_reason"}

#: The dataset a candidate came from, carried through the filter untouched.
_TAG_KEY = "dataset_tag"


Decision = namedtuple("Decision", "key fields status reason")


class Selection(object):
    """Which record represents each dataset, and what beat the rest.

    The filter instance is kept rather than discarded: why a dataset was set
    aside belongs in the durable record, and a derived list has no room for it.
    """

    def __init__(self, winners, superseded, engine):
        self.winners = winners  # dataset tag -> the record that won
        self.superseded = superseded  # dataset tag -> label of what beat it
        self.engine = engine


class RunSurvey(object):
    """What the output tree says about datasets that left no record.

    Only the two facts that separate "never submitted" from "died before its
    first step": a rejection marker, and a working directory.
    """

    def __init__(self, process_dir):
        self.process_dir = Path(process_dir)
        self.rejections = self._read_rejections()

    def _read_rejections(self):
        rejected_dir = self.process_dir / ".rejected"
        if not rejected_dir.is_dir():
            return {}
        reasons = {}
        for marker in sorted(rejected_dir.glob("*.txt")):
            try:
                reasons[marker.stem] = marker.read_text().strip()
            except OSError:
                reasons[marker.stem] = "(could not read the rejection marker)"
        return reasons

    def started(self, dataset_key):
        return (self.process_dir / dataset_key).is_dir()


class GatherReport(object):
    """What the gather did, in the terms the stage log reports it in."""

    def __init__(self, decisions, damaged):
        self.decisions = decisions
        self.damaged = damaged

    @property
    def processed(self):
        return sum(1 for d in self.decisions if d.status == STATUS_PROCESSED)

    @property
    def failed(self):
        return sum(1 for d in self.decisions if d.status == STATUS_FAILED)

    def summary(self):
        line = (
            f"{self.processed} of {len(self.decisions)} datasets selected "
            f"for refinement"
        )
        if self.damaged:
            line += f"; {len(self.damaged)} unreadable record(s)"
        return line


# --------------------------------------------------------------------------- #
# Gather                                                                       #
# --------------------------------------------------------------------------- #


def collect(process_dir):
    """Load every per-process record under *process_dir*.

    Returns ``({dataset tag: [record, ...]}, [(dataset tag, preserved file)])``.
    A record is returned under the terms it is kept as, whatever the producer
    spelled it.  The tag of an unreadable record still comes back, because the
    file is named for its dataset.
    """
    records, damaged = {}, []
    for path in sorted(Path(process_dir).rglob(PROCESS_PATTERN)):
        record = ProcessInfo.load(path)
        try:
            key = record.key
        except ValueError:
            continue
        fields = record.datasets.get(key)
        if fields is None:
            preserved = (record.recovered or {}).get("damaged_copy")
            damaged.append((key, preserved or path.name))
            continue
        records.setdefault(key, []).append(ProcessingTerms.inbound(fields))
    return records, damaged


# --------------------------------------------------------------------------- #
# Select                                                                       #
# --------------------------------------------------------------------------- #


def _candidate(dataset_key, fields, crystal_code):
    """One record in the shape the result filter reads.

    ``dataset_code`` is dropped rather than translated: it names the pin folder
    on the filter's side and the dataset tag on this one.  The crystal's
    identity is supplied explicitly instead.
    """
    candidate = ProcessingTerms.outbound(fields)
    candidate.pop("dataset_code", None)
    candidate["crystal_code"] = crystal_code or dataset_key
    candidate[_TAG_KEY] = dataset_key
    return candidate


def select(records, crystal_codes, filter_class=ProcessingFilter):
    """Run the result filter over gathered records.

    *crystal_codes* maps dataset tag to crystal code, so two collections of one
    crystal are compared against each other even though their tags differ.
    """
    candidates, origin = [], {}
    for dataset_key, attempts in records.items():
        for fields in attempts:
            candidate = _candidate(dataset_key, fields, crystal_codes.get(dataset_key))
            origin[id(candidate)] = fields
            candidates.append(candidate)

    engine = filter_class()
    winners = engine.run(candidates)
    return Selection(
        winners={c[_TAG_KEY]: origin[id(c)] for c in winners},
        superseded={c[_TAG_KEY]: label for c, label in engine.superseded},
        engine=engine,
    )


# --------------------------------------------------------------------------- #
# Resolve                                                                      #
# --------------------------------------------------------------------------- #


def _representative(attempts):
    """The attempt a dataset is described by when none of them won.

    The one that got furthest, so the reason reported is the most informative
    of the ones available rather than whichever was globbed last.
    """
    return max(
        attempts,
        key=lambda f: (
            f.get("outcome") == OUTCOME_COMPLETED,
            len(f.get("steps") or []),
            f.get("finished") or f.get("started") or "",
        ),
    )


def _last_step(fields):
    steps = fields.get("steps") or []
    return steps[-1].get("id") if steps else None


def _why_not_selected(fields, superseded):
    """Why a dataset that has a record is not going on to the next stage.

    *superseded* is the label of the result that beat this one, or None."""
    outcome = fields.get("outcome")
    if outcome == OUTCOME_RUNNING:
        step = _last_step(fields)
        return (
            f"the job stopped after {step} and never finished"
            if step
            else "the job stopped before its first step finished"
        )
    if outcome in (OUTCOME_REJECTED, OUTCOME_UNPROCESSED):
        return fields.get("failure_reason") or f"the job ended {outcome}"
    if outcome != OUTCOME_COMPLETED:
        step = fields.get("failed_step")
        reason = fields.get("failure_reason") or "the job failed"
        return f"{step}: {reason}" if step else reason
    if superseded:
        # Naming the winner is the point: the loser is sound data, and the only
        # thing a reader needs is where the better measurement of that crystal
        # went.
        return f"superseded by {superseded}"
    if not fields.get("cc_half"):
        return "the job finished without producing scaling statistics"
    return "the scaling statistics did not meet the quality thresholds"


def resolve(dataset_keys, records, selection, damaged, survey):
    """One decision per dataset in the project record, in its order.

    A dataset with no record is still decided: it is in the run's account
    either way, and which of the states it is in is the useful part.
    """
    unreadable = dict(damaged)
    decisions = []
    for key in dataset_keys:
        attempts = records.get(key) or []
        if key in selection.winners:
            fields = dict(selection.winners[key])
            fields["attempts"] = len(attempts)
            decisions.append(Decision(key, fields, STATUS_PROCESSED, None))
            continue
        if attempts:
            fields = dict(_representative(attempts))
            fields["attempts"] = len(attempts)
            reason = _why_not_selected(fields, selection.superseded.get(key))
            decisions.append(Decision(key, fields, STATUS_FAILED, reason))
            continue
        if key in unreadable:
            fields = {"outcome": OUTCOME_UNPROCESSED}
            reason = (
                f"its record could not be read and was preserved as {unreadable[key]}"
            )
        elif key in survey.rejections:
            fields = {"outcome": OUTCOME_REJECTED}
            reason = survey.rejections[key]
        elif survey.started(key):
            fields = {"outcome": OUTCOME_UNPROCESSED}
            reason = "the job left no record -- it died before its first step"
        else:
            fields = {"outcome": OUTCOME_UNPROCESSED}
            reason = "no job ran for this dataset"
        decisions.append(Decision(key, fields, STATUS_FAILED, reason))
    return decisions


# --------------------------------------------------------------------------- #
# Merge                                                                        #
# --------------------------------------------------------------------------- #


def merge(info, decisions):
    """Apply *decisions* to the project record, in memory.

    Merged into what is already there, never over it: the project record holds
    the crystal identities and every earlier stage's fields, and this stage
    knows nothing about either.
    """
    for decision in decisions:
        fields = {
            name: value
            for name, value in decision.fields.items()
            if name not in _PROJECT_OWNED
        }
        info.update(decision.key, **fields)
        if decision.status == STATUS_FAILED:
            info.fail(decision.key, decision.reason)
        else:
            info.set_status(decision.key, decision.status)
    return info


def mark_failed_stage(info, decisions, stage):
    """Record which stage failed each failed dataset, and clear it from the rest.

    Clearing is explicit because merge never removes a field: a mark left by an
    earlier failed attempt would otherwise outlive the success that replaced it.
    """
    for decision in decisions:
        record = info[decision.key]
        if decision.status == STATUS_FAILED:
            record[FAILED_STAGE] = stage
        else:
            record.pop(FAILED_STAGE, None)


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #


def _first(fields, name):
    value = fields.get(name)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return "-" if value in (None, "") else str(value)


def duplicate_report(info, decisions):
    """Lines listing every crystal collected more than once, and which of its
    datasets was kept.

    Plain text with no log prefix, so the block reads and copies as a table.
    Empty when no crystal was collected twice.
    """
    groups = {}
    for decision in decisions:
        code = info[decision.key].get("crystal_code") or decision.key
        groups.setdefault(code, []).append(decision)
    duplicates = {code: group for code, group in groups.items() if len(group) > 1}
    if not duplicates:
        return []

    width = max(len(d.key) for group in duplicates.values() for d in group)
    lines = [
        f"Duplicate collections: {len(duplicates)} crystal(s) collected more "
        f"than once; one dataset kept per crystal",
        "(* = selected; ranked by overall Rpim, then CC1/2, then resolution)",
        "",
        f"    {'dataset':<{width}}  {'res':>5}  {'Rpim':>6}  {'CC1/2':>6}  note",
    ]
    for code, group in duplicates.items():
        lines.append(code)
        # Tags carry the collection date, so this order shows which collection
        # won; a loss to another collection needs no note beside the marker.
        for d in sorted(group, key=lambda d: d.key):
            selected = d.status == STATUS_PROCESSED
            if selected:
                note = "selected"
            elif d.reason and d.reason.startswith("superseded by "):
                note = ""
            else:
                note = d.reason or ""
            lines.append(
                f"  {'*' if selected else ' '} {d.key:<{width}}  "
                f"{_first(d.fields, 'high_res_limit'):>5}  "
                f"{_first(d.fields, 'rpim'):>6}  "
                f"{_first(d.fields, 'cc_half'):>6}  {note}".rstrip()
            )
        if not any(d.status == STATUS_PROCESSED for d in group):
            lines.append("    (none selected)")
    return lines


class ProcessingGather(object):
    """The processing stage's gather, from the records on disk to info.json.

    The filter instance is kept on the gather for the same reason the selection
    keeps it: the decisions it made about competing collections of one crystal
    are wanted afterwards.
    """

    def __init__(self, process_dir):
        self.process_dir = Path(process_dir)
        self.filter = None

    def run(self, info):
        records, damaged = collect(self.process_dir)
        crystal_codes = {
            key: record.get("crystal_code") for key, record in info.datasets.items()
        }
        selection = select(records, crystal_codes)
        self.filter = selection.engine
        decisions = resolve(
            list(info.datasets),
            records,
            selection,
            damaged,
            RunSurvey(self.process_dir),
        )
        merge(info, decisions)
        mark_failed_stage(info, decisions, STAGE_PROCESS)
        return GatherReport(decisions, damaged)


# --------------------------------------------------------------------------- #
# Refinement                                                                   #
# --------------------------------------------------------------------------- #
#
# One job per dataset, so there is nothing to select: each record either
# refined its dataset or says why not.  The fields merged into the project
# record are named for this stage, because the processing winner's own fields
# (its outcome, its steps, its job id) are already there under the plain names.


def _refined(fields):
    """The fields the project record keeps about one refinement job."""
    return {
        "rwork": fields.get("rwork"),
        "rfree": fields.get("rfree"),
        "refine_resolution": fields.get("refine_resolution"),
        "final_refined_pdb": fields.get("final_refined_pdb"),
        "final_refined_mtz": fields.get("final_refined_mtz"),
        "refine_job_dir": fields.get("job_dir"),
        "refine_outcome": fields.get("outcome"),
        "refine_failed_step": fields.get("failed_step"),
    }


#: Every field refinement adds to a project record.
REFINE_FIELDS = tuple(_refined({}))


#: Every field analysis adds to a project record.
ANALYSIS_FIELDS = ("event_found", "n_events", "pandda_dtag", "ligand_codes")


def _reset_stage(info, reached, failed_stages, fields, status):
    """Return to *status* every dataset that reached one of *reached* or
    failed at one of *failed_stages*, dropping *fields* and the failure mark;
    the keys reset, in file order.

    For a restart that deletes a stage's results: the fields would otherwise
    describe results that no longer exist, and merge never removes a field,
    so a dataset whose new job left no record would keep them.  A dataset
    that has moved past the stage goes back too, since what came after was
    built on the deleted results.
    """
    reset = []
    for key, record in info.datasets.items():
        current = record.get("status")
        stage_failure = (current == STATUS_FAILED
                         and record.get(FAILED_STAGE) in failed_stages)
        if current not in reached and not stage_failure:
            continue
        for name in fields + (FAILED_STAGE,):
            record.pop(name, None)
        info.set_status(key, status)
        reset.append(key)
    return reset


def reset_refinement(info):
    """Undo refinement and everything after it (``vic.refine --overwrite``)."""
    return _reset_stage(info, (STATUS_REFINED, STATUS_ANALYZED),
                        (STAGE_REFINE, STAGE_ANALYSIS),
                        REFINE_FIELDS + ANALYSIS_FIELDS, STATUS_PROCESSED)


def reset_analysis(info):
    """Undo analysis (``vic.analysis --overwrite``)."""
    return _reset_stage(info, (STATUS_ANALYZED,), (STAGE_ANALYSIS,),
                        ANALYSIS_FIELDS, STATUS_REFINED)


def _why_not_refined(fields):
    outcome = fields.get("outcome")
    if outcome == OUTCOME_RUNNING:
        step = _last_step(fields)
        return (
            f"refinement stopped after {step} and never finished"
            if step
            else "refinement stopped before its first step finished"
        )
    step = fields.get("failed_step")
    reason = fields.get("failure_reason") or "refinement failed"
    return f"{step}: {reason}" if step else reason


def collect_refinement(qrefine_dir):
    """``({dataset tag: record}, [(dataset tag, preserved file)])`` for every
    refinement job record under *qrefine_dir*, keyed by the tag the record
    carries as a field."""
    records, damaged = collect(qrefine_dir)
    by_tag = {}
    for key, attempts in records.items():
        for fields in attempts:
            tag = fields.get("dataset_tag") or key
            earlier = by_tag.get(tag)
            if earlier is None or (fields.get("started") or "") > (
                earlier.get("started") or ""
            ):
                by_tag[tag] = fields
    return by_tag, damaged


def resolve_refinement(dataset_keys, records, damaged, qrefine_dir):
    """One decision per dataset this refinement run was given."""
    unreadable = dict(damaged)
    decisions = []
    for key in dataset_keys:
        fields = records.get(key)
        if fields is not None and fields.get("outcome") == OUTCOME_COMPLETED:
            decisions.append(Decision(key, _refined(fields), STATUS_REFINED, None))
            continue
        if fields is not None:
            decisions.append(
                Decision(key, _refined(fields), STATUS_FAILED, _why_not_refined(fields))
            )
            continue
        if key in unreadable:
            reason = (
                f"its refinement record could not be read and was "
                f"preserved as {unreadable[key]}"
            )
        elif (Path(qrefine_dir) / key).is_dir():
            reason = (
                "the refinement job left no record -- it died before its first step"
            )
        else:
            reason = "no refinement job ran for this dataset"
        decisions.append(
            Decision(
                key, {"refine_outcome": OUTCOME_UNPROCESSED}, STATUS_FAILED, reason
            )
        )
    return decisions


class RefinementReport(GatherReport):
    @property
    def refined(self):
        return sum(1 for d in self.decisions if d.status == STATUS_REFINED)

    def summary(self):
        line = f"{self.refined} of {len(self.decisions)} datasets refined"
        if self.damaged:
            line += f"; {len(self.damaged)} unreadable record(s)"
        return line


class RefinementGather(object):
    """The refinement stage's gather, from the job records to info.json.

    *dataset_keys* are the datasets the run was given.  A key the project
    record does not hold (a hand-made override list can name one) is reported
    and left out rather than added as a dataset nobody collected.
    """

    def __init__(self, qrefine_dir, dataset_keys):
        self.qrefine_dir = Path(qrefine_dir)
        self.dataset_keys = list(dataset_keys)
        self.unknown = []

    def run(self, info):
        keys = [k for k in self.dataset_keys if k in info]
        self.unknown = [k for k in self.dataset_keys if k not in info]
        records, damaged = collect_refinement(self.qrefine_dir)
        decisions = resolve_refinement(keys, records, damaged, self.qrefine_dir)
        merge(info, decisions)
        mark_failed_stage(info, decisions, STAGE_REFINE)
        for decision in decisions:
            if decision.status != STATUS_FAILED:
                # Left by an earlier attempt that failed; merge never clears.
                info[decision.key].pop("refine_failed_step", None)
        return RefinementReport(decisions, damaged)


# --------------------------------------------------------------------------- #
# Analysis                                                                     #
# --------------------------------------------------------------------------- #
#
# pandda.analyse is one job over every dataset, and it either finishes or it
# does not.  Its datasets are known by panDDA's own codes, which the preparer
# records in info.json as ``pandda_dtag`` when it places them.

def placed_ligands(pandda_dir, dtag):
    """The ligand codes placed beside one panDDA dataset's model, sorted: the
    names of the restraint files actually there, so a ligand that could not be
    generated is not listed."""
    codes = set()
    for folder in Path(pandda_dir, "data").glob(f"*_{dtag}"):
        codes.update(cif.stem for cif in folder.glob("*.cif"))
    return sorted(codes)


def resolve_analysis(datasets, rows, complete, ligands=None):
    """One decision per ``(dataset tag, panDDA code)`` in *datasets*.

    *rows* are the per-dataset results of the panDDA summary, keyed by code;
    *ligands* the ligand codes placed for each code.
    """
    ligands = ligands or {}
    decisions = []
    for key, dtag in datasets:
        fields = {"pandda_dtag": dtag, "ligand_codes": ligands.get(dtag, [])}
        if not complete:
            decisions.append(Decision(key, fields, STATUS_FAILED,
                                      "pandda.analyse did not finish"))
            continue
        row = rows.get(dtag)
        if row is None:
            decisions.append(Decision(
                key, fields, STATUS_FAILED,
                f"{dtag} is not in the pandda.analyse summary"))
            continue
        if row["outcome"] == OUTCOME_COMPLETED:
            fields["event_found"] = bool(row["interesting"])
            fields["n_events"] = row["clusters"]
            decisions.append(Decision(key, fields, STATUS_ANALYZED, None))
            continue
        checks = row.get("checks") or ""
        reason = (f"pandda.analyse: {checks}" if checks and checks.lower() != "ok"
                  else f"pandda.analyse: {row['outcome']}")
        decisions.append(Decision(key, fields, STATUS_FAILED, reason))
    return decisions


class AnalysisReport(GatherReport):
    @property
    def analyzed(self):
        return sum(1 for d in self.decisions if d.status == STATUS_ANALYZED)

    @property
    def with_events(self):
        return sum(1 for d in self.decisions if d.fields.get("event_found"))

    def summary(self):
        return (f"{self.analyzed} of {len(self.decisions)} datasets analysed, "
                f"{self.with_events} with events")


class AnalysisGather(object):
    """The analysis stage's gather, from the panDDA output to info.json.

    *analysis_dir* holds this stage's ``pandda/`` folder.  The datasets are
    the analysed project's own, found by the ``pandda_dtag`` the preparer
    recorded: ground-state datasets come from elsewhere and are reference
    data here, not results.
    """

    def __init__(self, analysis_dir):
        self.analysis_dir = Path(analysis_dir)

    def run(self, info):
        from avicenna.utils.utils import AnalysisScraper

        backend = AnalysisScraper._BACKENDS["pandda"](self.analysis_dir)
        complete = backend.is_complete()
        rows = {}
        if complete:
            rows = {row["dtag"]: row for row in backend.parse()[0]}
        datasets = [(key, record["pandda_dtag"])
                    for key, record in info.datasets.items()
                    if record.get("pandda_dtag")]
        pandda_dir = self.analysis_dir / "pandda"
        ligands = {dtag: placed_ligands(pandda_dir, dtag) for _, dtag in datasets}
        decisions = resolve_analysis(datasets, rows, complete, ligands)
        merge(info, decisions)
        mark_failed_stage(info, decisions, STAGE_ANALYSIS)
        return AnalysisReport(decisions, [])


# --------------------------------------------------------------------------- #
# info.csv                                                                     #
# --------------------------------------------------------------------------- #

#: The columns of the project's info.csv, in order.
INFO_CSV_COLUMNS = ("dataset", "resolution", "ligand_name", "a", "b", "c",
                    "alpha", "beta", "gamma", "space_group", "smiles")

_CELL_COLUMNS = ("a", "b", "c", "alpha", "beta", "gamma")


def info_csv_rows(info):
    """One row per analysed dataset, in panDDA code order, derived from
    info.json alone.

    ``dataset`` is the panDDA code and ``resolution`` the overall
    high-resolution limit of the processed data.  ``ligand_name`` is
    ``<library>_<well>`` from the crystal list, without any stereoisomer
    suffix: software downstream resolves the isomer itself.
    """
    from avicenna.utils.ligands import ligand_name

    rows = []
    for record in info.datasets.values():
        if record.get("status") != STATUS_ANALYZED or not record.get("pandda_dtag"):
            continue
        resolution = record.get("high_res_limit")
        if isinstance(resolution, (list, tuple)):
            resolution = resolution[0] if resolution else ""
        cell = record.get("unit_cell")
        if not isinstance(cell, (list, tuple)) or len(cell) != len(_CELL_COLUMNS):
            cell = [""] * len(_CELL_COLUMNS)
        library, well = record.get("library_code"), record.get("well_code")
        row = {
            "dataset": record["pandda_dtag"],
            "resolution": resolution or "",
            "ligand_name": ligand_name(library, well) if library and well else "",
            "space_group": record.get("space_group") or "",
            "smiles": record.get("SMILES") or "",
        }
        row.update(zip(_CELL_COLUMNS, cell))
        rows.append(row)
    return sorted(rows, key=lambda row: row["dataset"])


def write_info_csv(info, path):
    """Write the project's info.csv from *info*; returns the number of rows."""
    import csv

    rows = info_csv_rows(info)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=INFO_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
