"""Real Fresh worker/preparation with controlled failures below their boundaries."""
import json
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import returning_listener, seed_menu_cards
from tests.recommendation_support import publish_bundles
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator
from yt_playlist.core.store import Store
from yt_playlist.controllers.pool_preparation import FreshAcquisition
from yt_playlist.rec import choice_evidence, measurement, mode_inputs, road_trip_candidates, taste_analysis
from yt_playlist.web.app import create_app


DIAGNOSTIC = 'internal-diagnostic-879-fresh: invalid stored ranking document'


@pytest.fixture(scope='module')
def quiet_schema():
    with bounded_consumer_schema(
            (('house', 'House'), ('jazz', 'Jazz'), ('techno', 'Techno')), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def quiet_pool(quiet_schema, tmp_path, monkeypatch, home_routes_only, without_metadata_providers):
    store = Store(tmp_path / 'quiet.db')
    with closing(store.conn):
        # Job/error behavior consumes uncontended SQL and completed cards.
        # Keep worker execution, publication files and HTTP rendering real.
        store.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(store, quiet_schema, snapshot_guards=False)
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        with mock_snapshot_coordinator(store, monkeypatch):
            selected = quiet_input(store)
            tracks = [track for ranked in selected.values() for track in ranked]
            assert len(tracks) == 12 and all(track['genre'] == 'Jazz' for track in tracks)
            assert len(store.rec_query.tracks_by_keys([track['key'] for track in tracks])) == len(tracks)
            assert store.discovery.get_discovered_tracks() == []
            assert not store.recommendations.page('fresh', 'all', 'fresh')
            yield from quiet_case(store, selected, monkeypatch)


def quiet_case(store, selected, monkeypatch):
    # Retry/error state does not depend on refingerprinting every fitted model
    # or collecting old policies. Their real owners are test_measurement_isolation
    # and test_measurement_storage. Policy records and publication remain real.
    monkeypatch.setattr(measurement, 'serving_policy', lambda owner, now, **kwargs:
        ('quiet-fresh-inputs', {'schema': 4, 'algorithm': 'quiet-fresh-fixture',
                              'genres': owner.rec_proposals.get_proposals('home_genre_request')}))
    monkeypatch.setattr(measurement, 'maintain_storage', lambda *args, **kwargs: None)
    # Choice learning is below the rendered job/error state and is not consumed
    # by these assertions. Actual capture has its own test_choice_evidence owners.
    monkeypatch.setattr(choice_evidence, 'present', lambda *args, **kwargs: None)
    # Explicit Fresh jobs remain real. Periodic model/choice workers own
    # separate tests and must not replace this controlled listener's history.
    account = store.accounts.get_accounts()[0].id
    app = create_app(store, lambda: {account: FakeClient()}, now_fn=lambda: 1000, background_work=False)
    http = TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop': True})
    ctx = http.app.state.ctx
    case = SimpleNamespace(store=store, http=http, ctx=ctx, fault=None, boundary='populate',
        calls=[], calculations=[], refreshes=[], schedules=[], entered=threading.Event(), release=threading.Event())
    case.release.set()
    local = threading.local()
    populate = FreshAcquisition.populate
    keys = store.discovery.discovered_track_keys

    def fail():
        case.entered.set()
        assert case.release.wait(10), 'Controlled Fresh worker was not released'
        if case.fault == 'json':
            raise json.JSONDecodeError(DIAGNOSTIC, '<html>invalid response</html>', 0)
        raise ValueError(DIAGNOSTIC)

    def observe_populate(acquisition, client, now, include=(), avoid=(), progress=lambda message: None):
        case.calls.append({'include_genres': include, 'avoid_genres': avoid})
        local.populating = True
        try:
            return populate(acquisition, client, now, include, avoid, progress)
        finally:
            local.populating = False

    def read_keys():
        if getattr(local, 'populating', False) and case.boundary == 'populate' and case.fault:
            fail()
        return keys()

    def calculate_bundles(reader, now, policies):
        case.calculations.append(True)
        if case.boundary == 'prepare' and case.fault:
            fail()
        # Error handling coordinates completed rankings; numerical rankers have
        # separate owners. Keep actual policy capture and SQL/file publication.
        pools = {lane: policies.stamp(tracks, lane) for lane, tracks in selected.items()}
        pools['fresh'] = []
        return {'all': pools, '_meta': {'full_pools': True, 'comfort_pool': 0}}, []

    monkeypatch.setattr(FreshAcquisition, 'populate', observe_populate)
    monkeypatch.setattr(store.discovery, 'discovered_track_keys', read_keys)
    monkeypatch.setattr(mode_inputs, '_calculate_bundles', calculate_bundles)
    # Genre research is the external acquisition boundary. No discoveries is a
    # real successful empty search; preparation/publication still run on recovery.
    monkeypatch.setattr(road_trip_candidates, 'genre_artists', lambda *args, **kwargs: [])
    def refresh(**kwargs):
        # Ordinary Home reads may schedule maintenance. A completed explicit
        # build forces publication; failed builds must never do that.
        (case.refreshes if kwargs.get('force') else case.schedules).append(kwargs)
    monkeypatch.setattr(ctx.rec_worker, 'trigger_home_refresh', refresh)
    # Reuse one real application/portal lifetime across polling and retries.
    with http:
        try:
            yield case
        finally:
            case.release.set()
            assert ctx.work.wait(10), 'Fresh work did not drain before fixture teardown'


def quiet_input(source):
    returning_listener(source, 'mixes', now=1000)
    source.accounts.upsert_account('main', None, True)
    cards = seed_menu_cards(source, count=3, tracks_per_card=4, genre='Jazz')
    pools = {card['lane']: card['tracks'] for card in cards}
    publish_bundles(source, {'all': pools}, 1000)
    taste_analysis.prepare_library_summary(source, 1000)
    return pools
