"""Pass-through phase measurements for genuine maximum-budget preparation."""
from functools import wraps
from dataclasses import replace
import time

import pytest

from yt_playlist.core import db_handles, fao, recommendation_outputs
from yt_playlist.core.preparation import clicked_publication, draft_publication, ranked_state, road_trip_drafts
from yt_playlist.core.preparation.mapped_publication import MappedPublication
from yt_playlist.core.preparation.ordered_rows import OrderedJob


@pytest.fixture
def preparation_costs(monkeypatch, record_property):
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
            (OrderedJob, 'step', 'worker_step'),
            (MappedPublication, 'step', 'map_step'),
            (MappedPublication, 'publish', 'publication'),
            (road_trip_drafts, 'describe', 'draft_describe'),
            (ranked_state, 'describe', 'ranked_describe'),
            (recommendation_outputs, 'encode', 'serialization'),
            (fao, '_write', 'artifact_write'),
            (db_handles.Cursor, '_run', 'sql_cursor')):
        monkeypatch.setattr(owner, attribute, timed(name, getattr(owner, attribute)))
    for module, name in ((draft_publication, 'draft'), (clicked_publication, 'ranked')):
        owner = module.OWNER
        monkeypatch.setattr(module, 'OWNER', replace(owner,
            verify_document=timed(name + '_verify', owner.verify_document),
            worker=replace(owner.worker, copy_batch=timed(name + '_members', owner.worker.copy_batch))))
    yield
    for name, (calls, wall, cpu) in sorted(costs.items()):
        record_property(name + '_calls', calls)
        record_property(name + '_wall_seconds', wall)
        record_property(name + '_thread_cpu_seconds', cpu)
