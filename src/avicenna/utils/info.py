"""info.json -- the record of what each dataset is, kept across sub-pipelines.

One file at the top of a project directory, created when processing starts and
added to as each sub-pipeline finishes.  It answers the question no single
sub-pipeline can: which crystal, which fragment, and how did it turn out.

Two records, one shape
----------------------
``ProjectInfo`` is that file.  ``ProcessInfo`` is the record a single worker
keeps of itself, in its own pipeline subdirectory, and there are several per
dataset -- one per crystal form, index range and forced-integration retry.  The
sentinel gathers them when the run finishes, selects between them and merges the
result into the project record.

Both are the same envelope over the same kind of datasets block, which is why
the merge has nothing to translate.  Everything about the file -- loading,
recovery, merging fields, writing atomically -- lives on the shared base, so a
change to it cannot reach one record and miss the other.

The project record has one writer at any time -- the CLI creates it, then each
stage's sentinel is the only thing that adds to it -- so no update can be lost
to a second writer.

Shape
-----
A ``version`` / ``meta`` envelope around the dataset dictionary, so a file that
outlives several releases can be recognised and, if it ever needs to be,
migrated.  ``meta`` also gives the crystal list path and the both-directions
unmatched report somewhere to live::

    {"version": 1,
     "meta": {"created": ..., "updated": ..., "avicenna_version": ...,
              "crystal_list": ..., "unmatched": {"folders": [...], "entries": [...]}},
     "datasets": {"<dataset tag>": {"crystal_code": ..., "SMILES": ..., ...}}}

Datasets are keyed by the tag the run produces, ``<collection>_<pin>_<sweep>``,
fixed when the file is created and never re-keyed: it is what every later stage
matches on.  The pin folder is not recoverable from the tag, so each record also
carries ``data_path`` and, to separate two sweeps of one folder, ``image_prefix``.

Writing, and surviving a damaged file
-------------------------------------
Reading and writing happens from compute nodes over NFS, at the end of a stage,
with a read-modify-write in between.  Each write is therefore atomic -- a
temporary file in the same directory, then ``os.replace`` -- and the project
record keeps the generation it supersedes as ``info.json.prev``.

A stage finalising a long run must not die because this file is damaged, but it
must not quietly replace it with a thinner one either: the result would be a
well-formed file that has silently lost the crystal identities and every earlier
stage's statistics, which reads as success.  So a damaged file is preserved
under a timestamped name, never overwriting an earlier one; the previous
generation is loaded in its place if it can be; and either way ``meta.recovered``
records what happened, so the degraded state is visible in the file itself rather
than only in a warning on a compute node.

info.json is additive metadata and never a precondition: a project with no
crystal list, or no info.json at all, processes exactly as it did before.  Its
contents can also be rebuilt -- the crystal fields from the crystal list and the
pin list, the statistics by re-scraping logs that are still on disk.
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as _package_version

FILENAME = "info.json"

# One worker process keeps its own record beside its logs, named for the dataset
# it is processing.  The name carries the tag so a file found on its own says
# what it belongs to, and so a gather pattern cannot pick up the project record.
PROCESS_SUFFIX = "_info.json"
PROCESS_PATTERN = "*" + PROCESS_SUFFIX

PREV_SUFFIX = ".prev"
BAD_SUFFIX = ".bad"
SCHEMA_VERSION = 1

# Fields that come from the crystal list.  A sub-pipeline adds its own keys
# beside them and must not change these: the crystal list is the identity of the
# crystal, and a stage that thinks it knows better is a stage with a bug.
CRYSTAL_LIST_FIELDS = ("crystal_code", "dataset_code", "library_code", "well_code",
                   "ligand_source", "SMILES", "compound_id")

# How far a dataset has got.  The first four are the stages it passes through,
# in order, each set once that stage has succeeded; "failed" is terminal and
# carries a reason.  Filtering counts as failing -- a dataset set aside because
# a better collection of the same crystal exists has not reached the next stage,
# and the reason says so.
#
# This is the switch that keeps a dataset out of generated input lists, and it
# is meant to be edited by hand as well as written by the pipeline: a dataset
# that should not be used for a reason no program can see (ice rings, a mount
# known to be wrong) has nowhere else to say so, and editing a derived list does
# not survive the next regeneration.
STATUS_IMPORTED = "imported"
STATUS_PROCESSED = "processed"
STATUS_REFINED = "refined"
STATUS_ANALYZED = "analyzed"
STATUS_FAILED = "failed"

STAGE_ORDER = (STATUS_IMPORTED, STATUS_PROCESSED, STATUS_REFINED, STATUS_ANALYZED)

# Which stage failed a dataset.  Every stage records it, and it matters to what
# may run next: a dataset failed by refinement is still a processing winner.
FAILED_STAGE = "failed_stage"
STAGE_PROCESS = "process"
STAGE_REFINE = "refine"
STAGE_ANALYSIS = "analysis"
STATUSES = STAGE_ORDER + (STATUS_FAILED,)

# How one worker PROCESS ended.  A separate axis from the dataset stage above:
# several processes (crystal form x index range, plus any forced-integration
# retries) run for a single dataset, and the sentinel selects among their
# records to decide what the dataset itself did.
#
# "running" is what a record says while its process is still going.  A record
# still saying so once the jobs are gone is a process that died -- and because
# the record is rewritten after every step, its step list says where.
OUTCOME_RUNNING     = "running"
OUTCOME_COMPLETED   = "completed"
OUTCOME_FAILED      = "failed"
OUTCOME_REJECTED    = "rejected"
OUTCOME_UNPROCESSED = "unprocessed"

OUTCOMES = (OUTCOME_RUNNING, OUTCOME_COMPLETED, OUTCOME_FAILED,
            OUTCOME_REJECTED, OUTCOME_UNPROCESSED)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _avicenna_version():
    try:
        return _package_version("avicenna")
    except PackageNotFoundError:
        return "unknown"


def resolve_path(path, basename=FILENAME):
    """Accept either a directory or a full path to the file itself.

    *basename* is None for a record whose file name depends on the dataset it
    describes, so a directory cannot be resolved without one.
    """
    path = str(path)
    if os.path.isdir(path):
        if not basename:
            raise ValueError(f"{path} is a directory, and this record's file "
                             f"name depends on its dataset -- pass the full "
                             f"path to the file")
        return os.path.join(path, basename)
    return path


def _warn(message):
    print(f"WARNING: {message}", file=sys.stderr, flush=True)


def _valid(data):
    """True if *data* is a record this module can work with."""
    return isinstance(data, dict) and isinstance(data.get("datasets"), dict)


def _read_json(path):
    """Return ``(data, None)`` or ``(None, reason it could not be used)``."""
    try:
        with open(path) as handle:
            data = json.load(handle)
    except OSError as exc:
        return None, f"could not be read ({exc})"
    except ValueError as exc:
        return None, f"is not valid JSON ({exc})"
    if not _valid(data):
        return None, "is not in the expected format"
    return data, None


def _preserve(path):
    """Move a damaged file aside under a name that cannot collide.

    Timestamped rather than a single ``.bad`` slot, because the second failure
    would otherwise destroy the evidence from the first -- which is the only
    remaining copy of whatever was in the file.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = f"{path}{BAD_SUFFIX}.{stamp}"
    serial = 0
    while os.path.exists(target):
        serial += 1
        target = f"{path}{BAD_SUFFIX}.{stamp}-{serial}"
    try:
        os.replace(path, target)
        return target
    except OSError as exc:
        _warn(f"{os.path.basename(path)} is damaged and could not be moved "
              f"aside ({exc}). Leaving it untouched.")
        return None


class InfoJSON(object):
    """The JSON envelope every avicenna record is written in.

    One file, one ``{"version", "meta", "datasets"}`` document: the project
    record (``ProjectInfo``) holds every dataset in the run, a worker's own
    record (``ProcessInfo``) holds the single dataset it is processing.  They
    are the same shape on purpose -- gathering per-process records into the
    project record is then a merge of one datasets block into another, with
    nothing to translate and so nothing that can drift apart.

    Everything to do with the file lives here: reading it, recovering a damaged
    one, merging fields into a dataset and writing atomically.  What the
    subclasses add is what their record is FOR.
    """

    #: File this kind of record lives in, or None when the name depends on the
    #: dataset it describes.
    BASENAME = FILENAME

    #: Whether ``write`` keeps the superseded generation as ``<file>.prev``.
    KEEP_PREVIOUS = True

    def __init__(self, path, data=None):
        self.path = resolve_path(path, self.BASENAME)
        self.data = data if data is not None else self._empty()

    # ------------------------------------------------------------------ #
    # Construction                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _empty():
        stamp = _now()
        return {"version": SCHEMA_VERSION,
                "meta": {"created": stamp,
                         "updated": stamp,
                         "avicenna_version": _avicenna_version()},
                "datasets": {}}

    @classmethod
    def load(cls, path):
        """Read the file, recovering as much as possible if it is damaged.

        Absent means a fresh, empty record -- the ordinary first-run case.
        Damaged means the file is preserved, the previous generation is used in
        its place if it is readable, and ``meta.recovered`` marks the result.
        A file written by a NEWER avicenna is kept and used as it stands:
        discarding it would throw away someone else's data over a version number.
        """
        path = resolve_path(path, cls.BASENAME)
        if not os.path.isfile(path):
            return cls(path)

        data, reason = _read_json(path)
        if data is not None:
            if data.get("version", 0) > SCHEMA_VERSION:
                _warn(f"{path} was written by a newer version of avicenna "
                      f"(format {data.get('version')}, this one understands "
                      f"{SCHEMA_VERSION}). Using it as it stands.")
            data.setdefault("meta", {})
            return cls(path, data)

        return cls._recover(path, reason)

    @classmethod
    def _recover(cls, path, reason):
        """Preserve a damaged file and fall back to the previous generation."""
        name = os.path.basename(path)
        damaged = _preserve(path)

        previous = path + PREV_SUFFIX
        recovered, prev_reason = (_read_json(previous)
                                  if os.path.isfile(previous) else (None, "is absent"))

        if recovered is not None:
            recovered.setdefault("meta", {})
            info = cls(path, recovered)
            source = os.path.basename(previous)
            lost = ("anything added since the previous stage wrote it")
        else:
            info = cls(path)
            source = None
            lost = ("everything it held; the preserved copy is the only record"
                    if damaged else "everything it held")

        info.meta["recovered"] = {
            "when": _now(),
            "from": source,
            "damaged_copy": os.path.basename(damaged) if damaged else None,
            "reason": f"{name} {reason}",
            "lost": lost,
        }
        _warn(f"{name} {reason}. "
              + (f"Preserved as {os.path.basename(damaged)}. " if damaged else "")
              + (f"Continuing from {source}." if source
                 else f"The previous generation {prev_reason}; starting empty.")
              + " See meta.recovered in the rebuilt file.")
        return info

    # ------------------------------------------------------------------ #
    # Access                                                              #
    # ------------------------------------------------------------------ #

    @property
    def datasets(self):
        return self.data["datasets"]

    @property
    def meta(self):
        return self.data.setdefault("meta", {})

    @property
    def recovered(self):
        """The ``meta.recovered`` stamp, or None for an undamaged record."""
        return self.meta.get("recovered")

    def __len__(self):
        return len(self.datasets)

    def __contains__(self, dataset_key):
        return dataset_key in self.datasets

    def __getitem__(self, dataset_key):
        return self.datasets[dataset_key]

    def get(self, dataset_key, default=None):
        return self.datasets.get(dataset_key, default)

    # ------------------------------------------------------------------ #
    # Update                                                              #
    # ------------------------------------------------------------------ #

    def update(self, dataset_key, **fields):
        """Merge *fields* into one dataset, creating it if it is new.

        Values of None are dropped, so a caller can pass a scraped statistic
        without first checking whether it was found.  Crystal-list fields
        that already hold a different value are kept and the attempt is warned
        about: a stage overwriting the crystal's identity is a bug, but not one
        worth killing a finalising sentinel over.  Nothing reaches disk until
        ``write`` is called.
        """
        record = self.datasets.setdefault(dataset_key, {})
        for key, value in fields.items():
            if value is None:
                continue
            if (key in CRYSTAL_LIST_FIELDS and record.get(key) not in ("", None)
                    and record[key] != value):
                _warn(f"refusing to change {key} of {dataset_key} from "
                      f"{record[key]!r} to {value!r} -- crystal-list fields "
                      f"are set once, when info.json is created.")
                continue
            record[key] = value
        return record

    def update_many(self, records):
        """Apply ``{dataset_key: {field: value}}`` in one pass."""
        for dataset_key, fields in records.items():
            self.update(dataset_key, **fields)

    # ------------------------------------------------------------------ #
    # Status                                                              #
    # ------------------------------------------------------------------ #

    def set_status(self, dataset_key, status, failure_reason=None):
        """Record how far a dataset has got, creating it if it is new.

        ``failure_reason`` is kept only for ``failed`` and is None otherwise, so
        a consumer can read the field unconditionally without having to know
        which statuses carry one.
        """
        if status not in STATUSES:
            raise ValueError(
                f"unknown status {status!r} for {dataset_key} "
                f"(expected one of: {', '.join(STATUSES)})")
        if status == STATUS_FAILED and not failure_reason:
            raise ValueError(f"{dataset_key} cannot be marked {STATUS_FAILED} "
                             f"without a failure_reason")
        record = self.datasets.setdefault(dataset_key, {})
        record["status"] = status
        record["failure_reason"] = failure_reason if status == STATUS_FAILED else None
        return record

    def fail(self, dataset_key, reason):
        """Mark a dataset failed, whatever the cause -- filtering included."""
        return self.set_status(dataset_key, STATUS_FAILED, failure_reason=reason)

    def status_of(self, dataset_key):
        return (self.get(dataset_key) or {}).get("status")

    # ------------------------------------------------------------------ #
    # Write                                                               #
    # ------------------------------------------------------------------ #

    def write(self):
        """Write the file atomically, keeping the superseded generation.

        The previous generation is snapshotted BEFORE the new file is put in
        place, so the record itself is never momentarily absent for a
        concurrent reader.  A snapshot that fails costs the fallback copy, not
        the write.
        """
        self.meta["updated"] = _now()
        self.data.setdefault("version", SCHEMA_VERSION)

        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)

        payload = json.dumps(self.data, indent=2, sort_keys=False) + "\n"
        if self.KEEP_PREVIOUS and os.path.isfile(self.path):
            self._snapshot_previous(directory)
        _atomic_write(self.path, payload, directory)
        return self.path

    def _snapshot_previous(self, directory):
        """Copy the file currently on disk to ``<file>.prev``."""
        try:
            with open(self.path) as handle:
                current = handle.read()
            _atomic_write(self.path + PREV_SUFFIX, current, directory)
        except OSError as exc:
            _warn(f"could not keep a previous copy of "
                  f"{os.path.basename(self.path)} ({exc}). The write itself "
                  f"is unaffected.")


class ProjectInfo(InfoJSON):
    """Every dataset in one project directory: ``info.json``.

    Created by ``vic.process`` before any job exists, so the run has an account
    of itself from the start, and added to by each stage as it finishes.  It is
    the only record a user or a later stage reads.
    """

    @classmethod
    def create_from(cls, path, datasets, result=None, ligands=None,
                    crystal_list=None):
        """Build a fresh record from discovered datasets and, if any, a crystal
        list.

        *datasets* is an ordered iterable of ``utils.DiscoveredDataset`` (key,
        folder_name, data_path, image_prefix), which fixes both the keys and the
        order of the datasets block, so it reads in pin order.  *result* is a
        ``crystal_list.MatchResult``, keyed by folder name; without one every
        dataset still gets its codes and simply carries no ligand fields, which
        is the no-crystal-list case and is not an error.
        """
        info = cls(path)
        matches = result.matches if result is not None else {}

        for dataset in datasets:
            info.data["datasets"][dataset.key] = _dataset_fields(
                dataset, matches.get(dataset.folder_name), ligands)

        meta = info.meta
        if crystal_list:
            meta["crystal_list"] = os.path.abspath(str(crystal_list))
        if ligands:
            meta["ligands"] = os.path.abspath(str(ligands))
        if result is not None:
            meta["unmatched"] = _unmatched_report(result)
        return info

    def reached(self, dataset_key, stage):
        """True if a dataset got at least as far as *stage* and did not fail.

        Stages are ordered, so asking for ``processed`` also accepts a dataset
        that has since been refined -- which is what generating an input list
        needs, since a dataset does not stop being processed once it moves on.
        """
        if stage not in STAGE_ORDER:
            raise ValueError(f"{stage!r} is not a pipeline stage "
                             f"(expected one of: {', '.join(STAGE_ORDER)})")
        status = self.status_of(dataset_key)
        if status not in STAGE_ORDER:
            return False              # failed, unset, or a value we do not know
        return STAGE_ORDER.index(status) >= STAGE_ORDER.index(stage)

    def select(self, stage=None):
        """Dataset keys fit to use, in file order.

        Without a *stage* this is every dataset that has not failed; with one it
        is every dataset that reached that stage.  Either way a failed dataset
        is never returned, which is what keeps it out of generated input lists.
        An unrecognised status is treated as unusable and warned about once, so
        a typo in a hand-edited file cannot quietly admit a dataset.
        """
        unknown = set()
        selected = []
        for key, record in self.datasets.items():
            status = record.get("status")
            if status is not None and status not in STATUSES:
                unknown.add(status)
                continue
            if status == STATUS_FAILED:
                continue
            if stage is not None and not self.reached(key, stage):
                continue
            selected.append(key)
        if unknown:
            _warn(f"{os.path.basename(self.path)} contains unrecognised status "
                  f"value(s) {', '.join(sorted(repr(u) for u in unknown))}; "
                  f"those datasets were skipped. Valid values: "
                  f"{', '.join(STATUSES)}.")
        return selected

    def select_for_refinement(self):
        """Dataset keys refinement takes as input, in file order.

        Every processing winner, including one whose refinement failed: a
        dataset failed by refinement has lost nothing processing decided, and
        leaving it out would mean it could never be refined again.
        """
        return [key for key, record in self.datasets.items()
                if self.reached(key, STATUS_PROCESSED)
                or (record.get("status") == STATUS_FAILED
                    and record.get(FAILED_STAGE) == STAGE_REFINE)]

    def select_for_analysis(self):
        """Dataset keys analysis takes as input, in file order.

        Every refined dataset, including one whose analysis failed, for the
        same reason as ``select_for_refinement``: pandda.analyse is all or
        nothing, so leaving those out would leave a failed run nothing to
        resume with.
        """
        return [key for key, record in self.datasets.items()
                if self.reached(key, STATUS_REFINED)
                or (record.get("status") == STATUS_FAILED
                    and record.get(FAILED_STAGE) == STAGE_ANALYSIS)]

    def failures(self):
        """``{dataset key: failure reason}`` for every failed dataset."""
        return {key: record.get("failure_reason")
                for key, record in self.datasets.items()
                if record.get("status") == STATUS_FAILED}

    def rename(self, old_key, new_key):
        """Move a dataset to a new key, keeping its fields.

        Datasets are keyed by data folder when the record is created, because
        sweeps are not known until the frames have been looked at.  Processing
        re-keys them to the real dataset tag once they are.
        """
        if old_key not in self.datasets or old_key == new_key:
            return
        record = self.datasets.pop(old_key)
        record["dataset_code"] = new_key
        self.datasets[new_key] = record

class ProcessInfo(InfoJSON):
    """What one worker process records about itself: ``<tag>_info.json``.

    It lives in the worker's own pipeline subdirectory, holds the single
    dataset that worker is processing, and is rewritten after every step.  A
    dataset normally has several of these -- one per crystal form, index range
    and forced-integration retry -- and the sentinel gathers them, selects
    between them and merges the winner into the project record.

    Rewriting after every step is what makes a killed worker legible: the stall
    watchdog hard-exits and ``h_rt``/``qdel`` are signals, so a record written
    only at the end would be missing from exactly the jobs worth inspecting.
    Each write lands atomically, so the file is never empty or half-written --
    no file means no step finished, and a file present describes progress
    through the last step that did.

    No ``.prev`` generation is kept: every write is the same record with more
    in it, so the superseded copy would hold strictly less.
    """

    BASENAME = None
    KEEP_PREVIOUS = False

    @classmethod
    def file_name(cls, dataset_key):
        return f"{dataset_key}{PROCESS_SUFFIX}"

    @classmethod
    def path_in(cls, directory, dataset_key):
        return os.path.join(str(directory), cls.file_name(dataset_key))

    @classmethod
    def key_from_path(cls, path):
        """The dataset tag a record file is named for, or None.

        The tag is in the name as well as in the record, so a file whose
        contents were lost can still be attributed to its dataset.
        """
        name = os.path.basename(str(path))
        if not name.endswith(PROCESS_SUFFIX) or len(name) <= len(PROCESS_SUFFIX):
            return None
        return name[:-len(PROCESS_SUFFIX)]

    @classmethod
    def start(cls, directory, dataset_key, **fields):
        """A fresh record for a worker that is about to begin."""
        info = cls(cls.path_in(directory, dataset_key))
        info.datasets[dataset_key] = {"dataset_code": dataset_key,
                                      "outcome": OUTCOME_RUNNING,
                                      "started": _now(),
                                      "steps": []}
        if fields:
            info.update(dataset_key, **fields)
        return info

    @property
    def key(self):
        """The dataset tag this record describes."""
        for key in self.datasets:
            return key
        key = self.key_from_path(self.path)
        if key is None:
            raise ValueError(f"{self.path} holds no dataset and is not named "
                             f"for one, so it cannot be attributed")
        return key

    @property
    def record(self):
        """The single dataset record, created if the file was lost."""
        return self.datasets.setdefault(self.key, {})

    def note_step(self, step_id, program=None, status=None, message=None,
                  seconds=None):
        """Append one finished step.  Only steps that actually ran are noted --
        a step skipped because an earlier one failed is a consequence, and the
        pipeline log already carries it."""
        entry = {"id": step_id, "program": program, "status": status,
                 "message": message}
        if seconds is not None:
            entry["seconds"] = round(float(seconds), 1)
        self.record.setdefault("steps", []).append(entry)
        return entry

    def set_outcome(self, outcome, failed_step=None, reason=None):
        """Record how this process ended.

        ``failed_step`` is its own field rather than only a phrase inside the
        reason: forty datasets stopping at the same step is a pattern, and
        forty sentences are not.  Both are set unconditionally so a consumer
        never has to know which outcomes carry them.
        """
        if outcome not in OUTCOMES:
            raise ValueError(
                f"unknown outcome {outcome!r} for {self.path} "
                f"(expected one of: {', '.join(OUTCOMES)})")
        record = self.record
        record["outcome"] = outcome
        record["failed_step"] = failed_step
        record["failure_reason"] = reason
        if outcome != OUTCOME_RUNNING:
            record["finished"] = _now()
        return record


def _atomic_write(path, text, directory):
    """Write *text* to *path* via a temporary file in the same directory."""
    handle = tempfile.NamedTemporaryFile(
        "w", dir=directory, prefix="." + os.path.basename(path) + ".",
        suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _dataset_fields(dataset, entry, ligands):
    """The initial record for one dataset.

    Without a crystal list entry the crystal code falls back to the folder name:
    the dataset is still a dataset, it simply has no fragment attached to it.
    Empty ligand fields are omitted rather than written blank, so a consumer can
    test for presence instead of for emptiness.
    """
    record = {"crystal_code": entry.crystal_code if entry else dataset.folder_name,
              "dataset_code": dataset.key,
              "data_path": dataset.data_path,
              "image_prefix": dataset.image_prefix,
              "status": STATUS_IMPORTED,
              "failure_reason": None}
    if entry is None:
        return record
    for key, value in (("library_code", entry.library),
                       ("well_code", entry.well),
                       ("SMILES", entry.smiles),
                       ("compound_id", entry.compound_id)):
        if value:
            record[key] = value
    source = entry.ligand_source(ligands)
    if source:
        record["ligand_source"] = source
    return record


def _unmatched_report(result):
    """Both directions of the crystal list mismatch, in full.

    The printed report caps one direction to stay readable; this does not, since
    the point of keeping it is to be able to go back and see what was skipped.
    """
    report = {"folders": list(result.unmatched_folders),
              "entries": [entry.crystal_code
                          for entry in result.unmatched_entries]}
    if result.unparsed_folders:
        report["unparsed_folders"] = list(result.unparsed_folders)
    if result.unparsed_entries:
        report["unparsed_entries"] = [entry.crystal_code
                                      for entry in result.unparsed_entries]
    if result.duplicate_entries:
        report["duplicate_entries"] = [entry.crystal_code
                                       for entry, _first in result.duplicate_entries]
    return report
