"""A failed actual-data audit must keep enough evidence to diagnose cold startup."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests import bench_progressive_recovery as audit


def _preparation(monkeypatch, *, finish_after=None, fail_after=None):
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(audit.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(audit.time, 'process_time', lambda: clock.now)
    monkeypatch.setattr(audit.time, 'thread_time', lambda: clock.now)
    monkeypatch.setattr(audit.musical_preparation, 'due', lambda store: True)
    monkeypatch.setattr(audit.vibe_preparation, 'due', lambda store: True)

    class Prepared:
        captures = 0
        coverage_reads = 0
        vectors = 0

        def has_pending(self):
            return finish_after is None or self.captures < finish_after

        def missing_unqueued(self, limit):
            self.coverage_reads += 1
            return []

        def missing_previous_inputs(self):
            return False

        def progress(self):
            return {'pending': 100 if self.has_pending() else 0, 'prepared': self.captures * 32,
                    'library_vectors': self.vectors, 'discovered_vectors': 0}

    prepared = Prepared()

    @contextmanager
    def read_view():
        yield store

    store = SimpleNamespace(prepared_content=prepared, read_view=read_view)

    class Preparation:
        def __init__(self):
            self.store = store

        def capture(self, now):
            clock.now += 60
            prepared.captures += 1
            if prepared.captures == fail_after:
                raise RuntimeError('controlled capture failure')

        def fit(self, now):
            prepared.vectors = prepared.captures * 32
            return True

        def status(self):
            return {**prepared.progress(), 'basis': {'rows': prepared.vectors}}

    return Preparation(), prepared


def test_capture_deadline_keeps_first_useful_timing_and_pending_work(monkeypatch):
    preparation, prepared = _preparation(monkeypatch)
    report, costs = {}, audit._Costs()
    with pytest.raises(audit._CaptureIncomplete, match='180 seconds'):
        audit._capture(preparation, 'cold-and-growing', 1000, report, costs)
    capture = report['capture']
    assert not capture['complete'] and capture['limit_s'] == 180
    assert capture['first_useful']['wall_s'] == 60
    assert capture['first_useful']['library_vectors'] == 32
    assert capture['pending'] == 100 and capture['prepared'] == 96
    assert capture['missing_sample'] == [] and not capture['missing_previous']
    assert capture['musical_sources_due'] and capture['vibe_due']
    assert costs.stages['capture.total']['wall_s'] == 180
    assert costs.stages['capture.total']['calls'] == 3
    # Completion probes no longer scan the full catalog before every batch.
    assert prepared.coverage_reads == 1


def test_capture_completion_checks_full_coverage_after_pending_work(monkeypatch):
    preparation, prepared = _preparation(monkeypatch, finish_after=2)
    report = {}
    assert audit._capture(preparation, 'cold-and-growing', 1000, report, audit._Costs()) == 2
    capture = report['capture']
    assert capture['complete'] and capture['prepared'] == 64 and capture['pending'] == 0
    assert capture['first_useful']['wall_s'] == 60
    assert prepared.coverage_reads == 2  # Completion plus final diagnostics.


def test_capture_exception_keeps_first_useful_timing_and_stage_cost(monkeypatch):
    preparation, _prepared = _preparation(monkeypatch, fail_after=2)
    report, costs = {}, audit._Costs()
    with pytest.raises(RuntimeError, match='controlled capture failure'):
        audit._capture(preparation, 'cold-and-growing', 1000, report, costs)
    assert report['capture']['first_useful']['wall_s'] == 60
    assert report['capture']['pending'] == 100
    assert report['capture']['wall_s'] == 120
    assert costs.stages['capture.total']['calls'] == 2
