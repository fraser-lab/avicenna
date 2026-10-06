"""The completion sentinel's signal to the foreground.

The DONE marker is the only thing the foreground reads, so it has to mean that
finalization actually happened.  A finalization that fails must leave no marker,
say so where somebody will look, and still release the run lock -- the cluster
jobs are gone either way.
"""

import json

import pytest

from avicenna.pipelines.sentinel import PipelineSentinel, ProcessingSentinel
from avicenna.utils.info import STATUS_PROCESSED
from avicenna.utils.utils import (SENTINEL_CONTEXT, SENTINEL_DONE,
                                  SENTINEL_FAILED, SENTINEL_WAIT_DONE,
                                  SENTINEL_WAIT_FAILED, SENTINEL_WAIT_TIMEOUT,
                                  format_log_line, log_offset,
                                  wait_for_sentinel)

from tests.test_gather import GOOD_STATS, TAG, write_record
from tests.test_gather import project  # noqa: F401  (fixture)


class _IdleWatcher:
    """Reports the sub-pipeline finished, so the sentinel finalizes at once."""
    _OMNIBUS_LOG = "omnibus.log"

    def __init__(self, procpath):
        pass

    def is_running(self):
        return False


class _Sentinel(PipelineSentinel):
    WATCHER = _IdleWatcher
    POLL_INTERVAL = 0
    CONFIRM_INTERVAL = 0

    def __init__(self, project_dir, finalize=None):
        super().__init__(project_dir)
        self.finalize = finalize

    def on_complete(self):
        if self.finalize:
            self.finalize()


def _broken():
    raise RuntimeError("gather could not be imported")


@pytest.fixture
def released(monkeypatch):
    """Record lock releases instead of touching a real lock."""
    calls = []
    monkeypatch.setattr("avicenna.utils.runlock.RunLock.release_for",
                        classmethod(lambda cls, d, j: calls.append(d) or True))
    return calls


def _wait(omnibus):
    return wait_for_sentinel(omnibus, poll=0.01, timeout=0.05)


# --------------------------------------------------------------------------- #
# The writer                                                                   #
# --------------------------------------------------------------------------- #

def test_a_finalization_that_succeeds_is_marked_done(tmp_path, released):
    sentinel = _Sentinel(tmp_path)
    sentinel.run()

    assert SENTINEL_DONE in sentinel.omnibus.read_text()
    assert _wait(sentinel.omnibus) == SENTINEL_WAIT_DONE
    assert released == [tmp_path.resolve()]


def test_a_finalization_that_fails_leaves_no_done_marker(tmp_path, released):
    sentinel = _Sentinel(tmp_path, finalize=_broken)
    with pytest.raises(RuntimeError):
        sentinel.run()

    text = sentinel.omnibus.read_text()
    assert SENTINEL_DONE not in text
    assert SENTINEL_FAILED in text
    assert "RuntimeError: gather could not be imported" in text


def test_a_failed_finalization_reaches_the_foreground_as_failed(tmp_path,
                                                                released):
    sentinel = _Sentinel(tmp_path, finalize=_broken)
    with pytest.raises(RuntimeError):
        sentinel.run()

    assert _wait(sentinel.omnibus) == SENTINEL_WAIT_FAILED


def test_the_lock_is_released_even_when_finalization_fails(tmp_path, released):
    sentinel = _Sentinel(tmp_path, finalize=_broken)
    with pytest.raises(RuntimeError):
        sentinel.run()

    assert released == [tmp_path.resolve()]


# --------------------------------------------------------------------------- #
# The reader                                                                   #
# --------------------------------------------------------------------------- #

def test_a_progress_line_from_the_sentinel_is_not_an_outcome(tmp_path):
    omnibus = tmp_path / "omnibus.log"
    omnibus.write_text(format_log_line("INFO", "info.json: 3 of 4 processed",
                                       context=SENTINEL_CONTEXT))

    assert _wait(omnibus) == SENTINEL_WAIT_TIMEOUT


def test_the_marker_phrase_from_another_source_is_not_an_outcome(tmp_path):
    omnibus = tmp_path / "omnibus.log"
    omnibus.write_text(format_log_line("INFO", SENTINEL_DONE, context="7654321"))

    assert _wait(omnibus) == SENTINEL_WAIT_TIMEOUT


def test_an_absent_log_is_a_wait_not_an_error(tmp_path):
    assert _wait(tmp_path / "omnibus.log") == SENTINEL_WAIT_TIMEOUT


def _terminal(message):
    return format_log_line("DONE", message, context=SENTINEL_CONTEXT)


def test_a_previous_runs_outcome_is_not_this_runs(tmp_path):
    """The log is appended to across runs in one directory, so an earlier
    sentinel's marker must not end the wait for this one."""
    omnibus = tmp_path / "omnibus.log"
    omnibus.write_text(_terminal(SENTINEL_DONE))
    start = log_offset(omnibus)

    assert wait_for_sentinel(omnibus, poll=0.01, timeout=0.05,
                             start=start) == SENTINEL_WAIT_TIMEOUT


def test_this_runs_outcome_is_found_past_an_earlier_one(tmp_path):
    omnibus = tmp_path / "omnibus.log"
    omnibus.write_text(_terminal(SENTINEL_FAILED))
    start = log_offset(omnibus)
    with open(omnibus, "a") as fh:
        fh.write(_terminal(SENTINEL_DONE))

    assert wait_for_sentinel(omnibus, poll=0.01, timeout=0.05,
                             start=start) == SENTINEL_WAIT_DONE


def test_a_log_replaced_since_the_offset_is_read_from_the_top(tmp_path):
    omnibus = tmp_path / "omnibus.log"
    omnibus.write_text("x" * 4096)
    start = log_offset(omnibus)
    omnibus.write_text(_terminal(SENTINEL_DONE))

    assert wait_for_sentinel(omnibus, poll=0.01, timeout=0.05,
                             start=start) == SENTINEL_WAIT_DONE


def test_an_absent_log_starts_at_the_top(tmp_path):
    assert log_offset(tmp_path / "omnibus.log") == 0


def test_a_log_with_non_ascii_text_is_still_read(tmp_path):
    """Scaling summaries carry a unit glyph, and the offset is in bytes."""
    omnibus = tmp_path / "omnibus.log"
    omnibus.write_bytes("res=1.85\u00c5\n".encode("utf-8"))
    start = log_offset(omnibus)
    with open(omnibus, "a", encoding="utf-8") as fh:
        fh.write("res=1.90\u00c5\n" + _terminal(SENTINEL_DONE))

    assert wait_for_sentinel(omnibus, poll=0.01, timeout=0.05,
                             start=start) == SENTINEL_WAIT_DONE


# --------------------------------------------------------------------------- #
# Processing: the project record comes first                                   #
# --------------------------------------------------------------------------- #

def _processing_sentinel(project_dir, monkeypatch):
    sentinel = ProcessingSentinel(project_dir)
    monkeypatch.setattr(sentinel, "_watcher", lambda: _IdleWatcher(None))
    monkeypatch.setattr(sentinel, "POLL_INTERVAL", 0)
    monkeypatch.setattr(sentinel, "CONFIRM_INTERVAL", 0)
    return sentinel


def test_a_failure_in_the_derived_files_still_leaves_the_record(project,
                                                                monkeypatch,
                                                                released):
    """info.json is gathered before the refinement list and .phil, so a run
    whose later finalization breaks still has its account of itself."""
    write_record(project, TAG, stats=GOOD_STATS)
    monkeypatch.setattr("avicenna.utils.utils.export_qrefine_list",
                        lambda *a, **k: _broken())
    sentinel = _processing_sentinel(project, monkeypatch)
    with pytest.raises(RuntimeError):
        sentinel.run()

    on_disk = json.loads((project / "info.json").read_text())
    assert on_disk["datasets"][TAG]["status"] == STATUS_PROCESSED
    assert SENTINEL_DONE not in sentinel.omnibus.read_text()


def test_a_gather_that_fails_is_a_failed_finalization(project, monkeypatch,
                                                      released):
    monkeypatch.setattr("avicenna.pipelines.gather.ProcessingGather.run",
                        lambda self, info: _broken())
    sentinel = _processing_sentinel(project, monkeypatch)
    with pytest.raises(RuntimeError):
        sentinel.run()

    assert _wait(sentinel.omnibus) == SENTINEL_WAIT_FAILED


def test_the_refinement_list_is_exported_from_the_winners(project, monkeypatch,
                                                          released):
    """qrefine_list.txt is an export of what refinement will read from
    info.json, so the two cannot disagree."""
    write_record(project, TAG, stats=GOOD_STATS)
    sentinel = _processing_sentinel(project, monkeypatch)
    sentinel.run()

    assert (project / "qrefine_list.txt").read_text() == \
        f"{TAG},1_P43212,refined,1 900,integration not forced\n"


def test_a_forced_winner_is_exported_as_forced(project):
    from avicenna.pipelines.gather import ProcessingGather
    from avicenna.utils.info import ProjectInfo
    from avicenna.utils.utils import export_qrefine_list
    write_record(project, TAG, index="index_1_900/1_P43212_force",
                 stats=GOOD_STATS)
    info = ProjectInfo.load(project)
    ProcessingGather(project / "processing").run(info)
    info.write()

    assert export_qrefine_list(project) == 1
    assert (project / "qrefine_list.txt").read_text() == \
        f"{TAG},1_P43212,refined,1 900,integration forced in P43212 1\n"


def test_the_refinement_sentinel_watches_the_runs_own_datasets(tmp_path):
    from avicenna.pipelines.sentinel import RefinementSentinel
    listed = RefinementSentinel(tmp_path, dataset_list="/abs/qrefine_list.txt")
    default = RefinementSentinel(tmp_path)

    assert listed._worker_kwargs() == {"dataset_list": "/abs/qrefine_list.txt"}
    assert default._worker_kwargs() == {}


def test_the_duplicate_report_is_set_off_from_the_log(tmp_path, monkeypatch,
                                                      released):
    """A block between rules, with no log prefix, so it can be read and
    copied as a table."""
    from tests.test_gather import _two_collections
    one, two = _two_collections(tmp_path)
    _processing_sentinel(tmp_path, monkeypatch).run()

    log = (tmp_path / "processing.log").read_text().splitlines()
    start = next(i for i, l in enumerate(log) if l.startswith("Duplicate collections"))
    end = next(i for i in range(start, len(log)) if set(log[i]) == {"="})

    assert set(log[start - 1]) == {"="}
    block = log[start:end]
    assert any(one.key in l and l.lstrip().startswith("*") for l in block)
    assert any(two.key in l and not l.lstrip().startswith("*") for l in block)
    assert not any(l.startswith("[") for l in block)


@pytest.mark.parametrize("sentinel_class", ["ProcessingSentinel", "RefinementSentinel"])
def test_the_foreground_prints_what_the_sentinel_wrote(tmp_path, monkeypatch,
                                                       capsys, sentinel_class):
    """Every sentinel's wait shows the terminal the lines it added to the
    omnibus log, and only those."""
    import avicenna.pipelines.sentinel as module
    sentinel = getattr(module, sentinel_class)(tmp_path)
    sentinel.omnibus.write_text("the foreground's own line\n")
    written = ("[ts] INFO  [ sentinel ]  info.json: 1 of 1 datasets selected\n"
               "=====\ntable\n=====\n"
               f"[ts] DONE  [ sentinel ]  {SENTINEL_DONE}\n")

    def finalize_on_the_cluster(path, start=0, **kwargs):
        with open(path, "a") as fh:
            fh.write(written)
        return "done"
    monkeypatch.setattr(module, "wait_for_sentinel", finalize_on_the_cluster)

    assert sentinel.wait() == "done"
    assert capsys.readouterr().out == written


def test_an_interrupted_wait_still_prints_what_arrived(tmp_path, monkeypatch,
                                                      capsys):
    import avicenna.pipelines.sentinel as module
    sentinel = module.ProcessingSentinel(tmp_path)

    def interrupted(path, start=0, **kwargs):
        with open(path, "a") as fh:
            fh.write("[ts] INFO  [ sentinel ]  partial\n")
        raise KeyboardInterrupt
    monkeypatch.setattr(module, "wait_for_sentinel", interrupted)

    with pytest.raises(KeyboardInterrupt):
        sentinel.wait()
    assert "partial" in capsys.readouterr().out
