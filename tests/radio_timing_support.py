"""Pass-through costs for real Radio preparation and continuing selection."""
from functools import wraps
import time

import pytest

from yt_playlist.core import db_handles, fao
from yt_playlist.rec import mode_surfaces
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections


@pytest.fixture(autouse=True)
def radio_costs(monkeypatch, record_property, request):
    totals = {}

    def timed(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return operation(*args, **kwargs)
            finally:
                values = totals.setdefault(name, [0, 0.0, 0.0])
                values[0] += 1
                values[1] += time.perf_counter() - wall
                values[2] += time.thread_time() - cpu
        return call

    for name in ('indexed', 'prepare', 'seed_catalog', 'catalog_track', 'prepare_profiles'):
        if hasattr(request.module, name):
            monkeypatch.setattr(request.module, name, timed(name, getattr(request.module, name)))
    for owner, attribute, name in (
            (RadioCandidates, '__init__', 'candidate_capture'),
            (RadioCandidates, 'next_track', 'next_track'),
            (RadioCandidates, 'direction_anchor', 'direction_anchor'),
            (RadioCandidates, '_read_vectors', 'vector_read'),
            (RadioDirections, 'update', 'directions'),
            (mode_surfaces, '_publish_typed_bundles', 'publication'),
            (db_handles.Cursor, '_run', 'sql_cursor'),
            (db_handles.Connection, 'commit', 'commit'),
            (fao, '_write', 'artifact_write')):
        monkeypatch.setattr(owner, attribute, timed(name, getattr(owner, attribute)))
    yield
    # Timers are inclusive, so phase totals overlap. Real I/O remains enabled.
    for name, (calls, wall, cpu) in sorted(totals.items()):
        record_property(name + '_calls', calls)
        record_property(name + '_wall_s', wall)
        record_property(name + '_thread_cpu_s', cpu)
