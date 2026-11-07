"""Real Fresh worker/preparation with controlled failures below their boundaries."""
import json
import threading
from types import SimpleNamespace

import pytest

from tests.home_fixture_support import prepared_client, returning_listener
from tests.prepared_fixture_support import prepared_template
from tests.test_home_other_genres import _publish_menu
from yt_playlist.rec import fresh_pool, home_cards, mode_surfaces, road_trip_candidates


DIAGNOSTIC = 'internal-diagnostic-879-fresh: invalid stored ranking document'


@pytest.fixture
def quiet_pool(store, quiet_home_template, monkeypatch, home_routes_only, without_metadata_providers):
    quiet_home_template.restore(store)
    # Recovery preserves a selected menu. Journey ordering has its own real
    # owners in test_journeys and does not affect worker failure or retry state.
    monkeypatch.setattr(home_cards, '_order_by_journey',
                        lambda owner, items, *args, **kwargs: list(items))
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
    from yt_playlist.rec import home_cache
    completed = home_cache.read(store, 'home_prepared')['menus'][0]['protos']

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

    def calculate_bundles(reader, now, policies):
        case.calculations.append(True)
        if case.boundary == 'prepare' and case.fault:
            fail()
        # Error handling coordinates completed rankings; numerical rankers have
        # separate owners. Keep actual policy capture and SQL/file publication.
        fields = ('key', 'video_id', 'title', 'artist', 'album', 'thumbnail', 'genre')
        pools = {card['lane']: policies.stamp(
                    [{field: track[field] for field in fields if field in track}
                     for track in card['tracks']], card['lane']) for card in completed}
        pools['fresh'] = []
        return {'all': pools, '_meta': {'full_pools': True, 'comfort_pool': 0}}, []

    monkeypatch.setattr(fresh_pool, 'populate', observe_populate)
    monkeypatch.setattr(store.discovery, 'discovered_track_keys', read_keys)
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate_bundles)
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


@pytest.fixture(scope='module')
def quiet_home_template(_pristine_current_schema, fresh_genre_candidates_template):
    def build(source):
        fresh_genre_candidates_template.restore(source)
        returning_listener(source, 'mixes', now=1000)
        source.accounts.upsert_account('main', 'cred', None, True)
        # Retain the real initial selection and SQL/file cache writer. These
        # recovery checks do not need to enumerate alternative worker menus.
        _publish_menu(source, 1000)

    with prepared_template(_pristine_current_schema, build) as template:
        yield template
