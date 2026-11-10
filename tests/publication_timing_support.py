"""Observe real publication work without substituting preparation or durability."""
from functools import wraps
import time

import pytest

from yt_playlist.core import db_handles, fao
from yt_playlist.core.migrations import regular, runner
from yt_playlist.core.preparation.document_publication import DocumentPublication
from yt_playlist.core.preparation.mapped_publication import MappedPublication
from yt_playlist.core.preparation.ordered_rows import OrderedJob
from yt_playlist.core.preparation.pair_publication import RetainedPublication
from yt_playlist.core.preparation.rows import Job
from yt_playlist.core.store import Store


@pytest.fixture
def publication_costs(monkeypatch, record_property, request):
    costs = {}

    def timed(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return operation(*args, **kwargs)
            finally:
                values = costs.setdefault(name, [0, 0.0, 0.0])
                values[0] += 1
                values[1] += time.perf_counter() - wall
                values[2] += time.thread_time() - cpu
        return call

    for owner, attribute, name in (
            (request.module, 'seed', 'seed'),
            (OrderedJob, 'step', 'preparation_step'),
            (Job, 'install', 'job_install'),
            (Job, 'validate', 'job_validate'),
            (Job, 'step', 'row_step'),
            (MappedPublication, 'step', 'map_step'),
            (MappedPublication, 'verify', 'map_verify'),
            (MappedPublication, 'publish', 'map_publish'),
            (RetainedPublication, 'step', 'retained_step'),
            (RetainedPublication, 'verify_roots', 'root_verify'),
            (RetainedPublication, 'publish', 'retained_publish'),
            (DocumentPublication, 'verify', 'document_verify'),
            (DocumentPublication, 'publish', 'document_publish'),
            (Store, 'init_schema', 'admission'),
            (regular, 'migrate', 'regular_migrate'),
            (runner, 'migrate', 'historical_replay'),
            (db_handles.Cursor, '_run', 'sql_cursor'),
            (db_handles.Connection, 'commit', 'commit'),
            (fao, '_write', 'artifact_write')):
        monkeypatch.setattr(owner, attribute, timed(name, getattr(owner, attribute)))
    yield
    # Nested/inclusive durations overlap; commit and artifact writes retain fsync.
    for name, (calls, wall, cpu) in sorted(costs.items()):
        record_property(name + '_calls', calls)
        record_property(name + '_wall_s', wall)
        record_property(name + '_thread_cpu_s', cpu)
