"""What an output folder already holds, before a new run starts in it.

A run into a folder that already has results is either finishing that earlier
run or starting over, and the user says which.  To tell them what is there, each
dataset in the input is put in one of three classes:

  * done -- rejected before submission, or every job the dataset ran left a
    record with a final outcome;
  * frayed -- the dataset was started but did not finish: a job left no record,
    a record still says ``running``, a record cannot be read, or a failed job
    handed over to a forced-integration retry that did not finish;
  * untouched -- nothing in the folder belongs to it.

A frayed dataset stopped for reasons that have nothing to do with its data
(a killed job, a deleted queue), so its partial results are not worth keeping.

The survey only reads.  A run that is refused has to leave the folder exactly
as the earlier run left it, so records are parsed directly rather than through
the record loader, which preserves a damaged file under a new name.
"""

import json
from pathlib import Path

from avicenna.pipelines.refinement_runner import load_datasets, refine_finished
from avicenna.utils.info import OUTCOMES, OUTCOME_RUNNING, ProcessInfo
from avicenna.utils.utils import enumerate_datasets

DONE = "done"
FRAYED = "frayed"
UNTOUCHED = "untouched"

RUN_COMPLETE = "complete"
RUN_INCOMPLETE = "incomplete"
RUN_NONE = "none"

_FINAL_OUTCOMES = frozenset(OUTCOMES) - {OUTCOME_RUNNING}


class RunSurveyResult(object):
    """The class of every dataset in the input, in input order."""

    def __init__(self, classes):
        self.classes = classes

    @property
    def total(self):
        return len(self.classes)

    @property
    def done(self):
        return sum(1 for c in self.classes.values() if c == DONE)

    @property
    def frayed(self):
        return [key for key, c in self.classes.items() if c == FRAYED]

    @property
    def state(self):
        values = set(self.classes.values())
        if values <= {UNTOUCHED}:
            return RUN_NONE
        if values == {DONE}:
            return RUN_COMPLETE
        return RUN_INCOMPLETE


def survey_run(pin_lines, process_dir, skip_prescans=True):
    """Classify every dataset the input would produce against *process_dir*.

    Datasets come from the same enumeration the project record is built from,
    so the count agrees with info.json.  Folders in *process_dir* that the
    input no longer names are not counted.
    """
    datasets, _, _ = enumerate_datasets(pin_lines, skip_prescans=skip_prescans)
    return classify([d.key for d in datasets], process_dir)


def classify(dataset_keys, process_dir):
    """The classification alone, over dataset tags already known."""
    process_dir = Path(process_dir)
    rejected = _rejections(process_dir)
    classes = {}
    for key in dataset_keys:
        dataset_dir = process_dir / key
        if dataset_dir.is_dir():
            classes[key] = DONE if _finished(dataset_dir, key) else FRAYED
        elif key in rejected:
            classes[key] = DONE
        else:
            classes[key] = UNTOUCHED
    return RunSurveyResult(classes)


def survey_refine(project_dir, dataset_list=None):
    """Classify every refinement job this run would submit.

    A job is done once it has produced the outputs the next stage reads; a
    folder without them is frayed, whether the job was killed or failed.  A
    missing list is not a run, so the caller fails on it as it would anyway.
    """
    try:
        datasets = load_datasets(project_dir, dataset_list)
    except OSError:
        return RunSurveyResult({})
    classes = {}
    for dataset in datasets:
        dataset.resolve()
        job_dir = dataset.job_dir
        if refine_finished(job_dir):
            classes[job_dir.name] = DONE
        elif job_dir.is_dir():
            classes[job_dir.name] = FRAYED
        else:
            classes[job_dir.name] = UNTOUCHED
    return RunSurveyResult(classes)


def _rejections(process_dir):
    rejected_dir = process_dir / ".rejected"
    if not rejected_dir.is_dir():
        return set()
    return {marker.stem for marker in rejected_dir.glob("*.txt")}


def _finished(dataset_dir, key):
    """Whether every job this dataset ran reached a final outcome."""
    job_dirs = sorted(dataset_dir.glob("*/index_*"))
    if not job_dirs:
        return False
    return all(_job_finished(job_dir, key) for job_dir in job_dirs)


def _job_finished(job_dir, key):
    record = _read_record(job_dir, key)
    if record is None or record.get("outcome") not in _FINAL_OUTCOMES:
        return False
    if not record.get("triggered_forced"):
        return True
    # A failure that handed over to a forced retry is only final once the
    # retry is.
    retries = sorted(p for p in job_dir.glob("*_force") if p.is_dir())
    if not retries:
        return False
    for retry_dir in retries:
        retry = _read_record(retry_dir, key)
        if retry is None or retry.get("outcome") not in _FINAL_OUTCOMES:
            return False
    return True


def _read_record(directory, key):
    """The dataset's record in *directory*, or None if absent or unreadable."""
    try:
        with open(ProcessInfo.path_in(directory, key)) as handle:
            data = json.load(handle)
        record = data["datasets"][key]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return record if isinstance(record, dict) else None
