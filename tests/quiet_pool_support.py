"""Real Fresh worker/preparation with controlled failures below their boundaries."""
import json
import threading
from types import SimpleNamespace

import pytest

from tests.home_fixture_support import prepared_client
from yt_playlist.rec import fresh_pool, mode_surfaces, road_trip_candidates


DIAGNOSTIC = 'internal-diagnostic-879-fresh: invalid stored ranking document'


@pytest.fixture
def quiet_pool(store, fresh_home_template, monkeypatch):
    fresh_home_template.restore(store)
    # Explicit Fresh jobs remain real. Periodic model/choice workers own
    # separate tests and must not replace this controlled listener's history.
    http = prepared_client(store, background_work=False)
    ctx = http.app.state.ctx
    case = SimpleNamespace(store=store, http=http, ctx=ctx, fault=None, boundary='populate',
        calls=[], calculations=[], refreshes=[], schedules=[], entered=threading.Event(), release=threading.Event())
    case.release.set()
    local = threading.local()
    populate = fresh_pool.populate
    keys = store.discovery.discovered_track_keys
    calculate = mode_surfaces._calculate_bundles

    def fail():
        case.entered.set()
        assert case.release.wait(10), 'Controlled Fresh worker was not released'
        if case.fault == 'json':
            raise json.JSONDecodeError(DIAGNOSTIC, '<html>invalid response</html>', 0)
        raise ValueError(DIAGNOSTIC)

    def observe_populate(owner, **parameters):
        case.calls.append({key: value for key, value in parameters.items() if key != 'on_progress'})
        local.populating = True
        try:
            return populate(owner, **parameters)
        finally:
            local.populating = False

    def read_keys():
        if getattr(local, 'populating', False) and case.boundary == 'populate' and case.fault:
            fail()
        return keys()

    def calculate_bundles(*args, **kwargs):
        case.calculations.append(True)
        if case.boundary == 'prepare' and case.fault:
            fail()
        return calculate(*args, **kwargs)

    monkeypatch.setattr(fresh_pool, 'populate', observe_populate)
    monkeypatch.setattr(store.discovery, 'discovered_track_keys', read_keys)
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate_bundles)
    # Genre research is the external acquisition boundary. No discoveries is a
    # real successful empty search; ranking/publication still run on recovery.
    monkeypatch.setattr(road_trip_candidates, 'genre_artists', lambda *args, **kwargs: [])
    def refresh(**kwargs):
        # Ordinary Home reads may schedule maintenance. A completed explicit
        # build forces publication; failed builds must never do that.
        (case.refreshes if kwargs.get('force') else case.schedules).append(kwargs)
    monkeypatch.setattr(ctx.rec_worker, 'trigger_home_refresh', refresh)
    try:
        yield case
    finally:
        case.release.set()
        assert ctx.work.wait(10), 'Fresh work did not drain before fixture teardown'
        http.close()
