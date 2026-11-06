"""Pass-through phase receipts for the real full-size TV cutover workflow."""
from contextlib import contextmanager
from dataclasses import replace
from functools import wraps
import time

import pytest

from yt_playlist.core import db_handles, fao, recommendation_outputs
from yt_playlist.core.preparation import tv_publication, tv_state
from yt_playlist.core.preparation.document_publication import DocumentPublication
from yt_playlist.core.preparation.ordered_rows import OrderedJob


@pytest.fixture
def tv_publication_costs(monkeypatch, record_property):
    costs = {}

    @contextmanager
    def measure(name):
        wall, cpu = time.perf_counter(), time.thread_time()
        try:
            yield
        finally:
            values = costs.setdefault(name, [0, 0.0, 0.0])
            values[0] += 1
            values[1] += time.perf_counter() - wall
            values[2] += time.thread_time() - cpu

    def timed(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            with measure(name):
                return operation(*args, **kwargs)
        return call

    for owner, attribute, name in (
            (OrderedJob, 'step', 'worker_step'),
            (DocumentPublication, 'verify', 'publication_proof'),
            (DocumentPublication, 'publish', 'publication'),
            (tv_state, 'resolve', 'reference_resolution'),
            (recommendation_outputs, 'encode', 'serialization'),
            (recommendation_outputs, 'publish', 'retained_file_publish'),
            (fao, '_write', 'artifact_write'),
            (db_handles.Cursor, '_run', 'sql_cursor')):
        monkeypatch.setattr(owner, attribute, timed(name, getattr(owner, attribute)))
    for attribute, name in (('describe', 'source_decode'), ('read_original', 'original_read')):
        operation = getattr(tv_publication.TVPreparation, attribute)
        monkeypatch.setattr(tv_publication.TVPreparation, attribute, staticmethod(timed(name, operation)))
    owner = tv_publication.OWNERS['continuations']
    monkeypatch.setitem(tv_publication.OWNERS, 'continuations', replace(owner,
        verify_rows=timed('typed_verification', owner.verify_rows),
        worker=replace(owner.worker, prepare=timed('plan_rebuild', owner.worker.prepare),
            copy_batch=timed('member_slice', owner.worker.copy_batch))))
    try:
        yield measure
    finally:
        for name, (calls, wall, cpu) in sorted(costs.items()):
            record_property(name + '_calls', calls)
            record_property(name + '_wall_seconds', wall)
            record_property(name + '_thread_cpu_seconds', cpu)
