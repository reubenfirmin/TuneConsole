"""Attribute actual candidate refresh work without replacing its operations."""
from functools import wraps
import time

import pytest

from tests.prepared_fixture_support import PreparedTemplate
from yt_playlist.core import db_handles, fao
from yt_playlist.rec import candidate_refresh, musical_character, vibe_pools


@pytest.fixture(scope='module')
def candidate_prefix_costs():
    return {}


@pytest.fixture(autouse=True)
def candidate_costs(monkeypatch, record_property, request, candidate_prefix_costs):
    totals = {}

    def timed(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return operation(*args, **kwargs)
            finally:
                value = totals.setdefault(name, [0, 0., 0.])
                value[0] += 1
                value[1] += time.perf_counter() - wall
                value[2] += time.thread_time() - cpu
        return call

    for name in ('seed', 'library', 'refresh', '_assert_library_matches', 'snooze'):
        if hasattr(request.module, name):
            monkeypatch.setattr(request.module, name, timed(name, getattr(request.module, name)))
    for owner, name, label in (
            (PreparedTemplate, 'restore', 'private_restore'),
            (candidate_refresh, 'preferences', 'preference_fingerprint'),
            (candidate_refresh, 'due', 'refresh_due'),
            (musical_character, 'load', 'musical_profile_read'),
            (vibe_pools, 'prepare', 'vibe_projection'),
            (fao, '_write', 'durable_artifact_write'),
            (db_handles.Connection, 'commit', 'commit')):
        monkeypatch.setattr(owner, name, timed(label, getattr(owner, name)))
    try:
        yield
    finally:
        # The module prefix may be built before function timers are installed.
        # Report its cold cost once; it belongs in the whole-cohort comparison.
        if candidate_prefix_costs and not candidate_prefix_costs.get('_reported'):
            for name, seconds in candidate_prefix_costs.items():
                record_property(name, seconds)
            candidate_prefix_costs['_reported'] = True
        for name, (calls, wall, cpu) in sorted(totals.items()):
            record_property(name + '_calls', calls)
            record_property(name + '_wall_s', wall)
            record_property(name + '_thread_cpu_s', cpu)
