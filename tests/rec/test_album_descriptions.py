import json
from types import SimpleNamespace

import pytest

from yt_playlist.core.store import Store
from yt_playlist.integrations.deepseek import DeepSeekIAO
from yt_playlist.providers import deepseek
from yt_playlist.rec import album_descriptions as copy

ALBUM = {'browse_id': 'first-id', 'artist': 'Patrice Bäumel',
         'title': 'Global Underground #42: Berlin (DJ Mix)', 'year': '2019'}


def own_album(store, album=ALBUM):
    """Observe an exact provider release before admitting any paid work."""
    payload = {'title': album['title'], 'year': album.get('year', '2019'),
        'artists': [{'name': album['artist']}], 'trackCount': 1,
        'tracks': [{'videoId': 'release-track', 'title': 'Release track',
                    'duration': '3:01', 'isExplicit': False}]}
    assert store.collection.observe_album_representation(album['browse_id'], payload, now=1)
    return album


def other_album(store, title):
    return own_album(store, {**ALBUM, 'browse_id': 'other-id', 'title': title})


@pytest.fixture(autouse=True)
def access_boundary(monkeypatch):
    monkeypatch.setattr(deepseek, '_iao', DeepSeekIAO())
    monkeypatch.setattr(deepseek._iao, 'pacer', SimpleNamespace(wait=lambda: None))
    monkeypatch.setattr(deepseek._iao, '_request', lambda *args, **kwargs: {})


@pytest.fixture
def generated(store, monkeypatch):
    own_album(store)
    monkeypatch.setattr(deepseek._iao, 'api_key', lambda *args: 'secret-key')
    calls = []
    def describe(album, send):
        calls.append(album)
        send({'controlled_album': album['browse_id']})
        return {'description': 'Persistent album copy.', 'confidence': 'moderate', 'uncertainty': '',
                'model': deepseek.MODEL, 'prompt_version': 1, 'usage': {'total_tokens': 319}}
    monkeypatch.setattr(deepseek, 'describe', describe)
    return calls


def test_refresh_alternate_id_disable_and_key_change_reuse_paid_copy(store, generated, monkeypatch):
    description, record = copy.get(store, ALBUM, 100)
    assert description['kind'] == 'generated' and not description['quote']
    assert record['usage']['total_tokens'] == 319
    other = {**ALBUM, 'browse_id': 'another-id', 'artist': 'Patrice Baumel', 'year': '2023'}
    # Names and years do not prove equivalence. Capture the same complete
    # provider representation under both IDs before reusing the paid result.
    own_album(store, {**ALBUM, 'browse_id': other['browse_id']})
    assert store.album_descriptions.album_id(other) == store.album_descriptions.album_id(ALBUM)
    assert copy.get(store, other, 200)[0] == description
    store.settings.set_setting('deepseek_album_descriptions_enabled', '0')
    monkeypatch.setattr(deepseek._iao, 'api_key', lambda *args: 'replacement-secret')
    assert copy.get(store, other, 999999)[0] == description
    assert copy.read(store, other) == description
    assert len(generated) == 1
    assert copy.identity(ALBUM) != copy.identity({**ALBUM, 'title': ALBUM['title'].replace('DJ Mix', 'Unmixed')})


def test_durable_copy_survives_database_reopen_and_home_cache_eviction(file_backed_store, generated):
    first = file_backed_store
    path = first._db_path
    own_album(first)
    description = copy.get(first, ALBUM, 100)[0]
    assert first.rec_proposals.publish_album_note(ALBUM['browse_id'], {'description': description}, 100)
    assert first.rec_proposals.get_proposals('album_notes')[ALBUM['browse_id']]['description'] == description
    for number in range(130):
        assert first.rec_proposals.publish_album_note(str(number), {'description': None}, 200)
    cache = first.rec_proposals.get_proposals('album_notes')
    assert len(cache) == 128 and ALBUM['browse_id'] not in cache
    first.conn.close()
    second = Store(path)
    try:
        second.init_schema()
        assert copy.get(second, ALBUM, 99999999)[0] == description
        assert len(generated) == 1
    finally:
        second.conn.close()


def test_daily_limit_counts_failures_survives_restart_and_does_not_reset_on_new_key(
        file_backed_store, generated, monkeypatch):
    store = file_backed_store
    path = store._db_path
    own_album(store)
    store.settings.set_setting('deepseek_daily_limit', '1')
    def fail(album, send):
        send({'controlled_album': album['browse_id']})
        raise deepseek.DescriptionUnavailable('unavailable')
    monkeypatch.setattr(deepseek, 'describe', fail)
    assert copy.get(store, ALBUM, 100)[1]['status'] == 'unavailable'
    store.conn.close()
    store = Store(path)
    try:
        store.init_schema()
        monkeypatch.setattr(deepseek._iao, 'api_key', lambda *args: 'another-key')
        store.rec_proposals.wake_album_descriptions(200)
        deferred = copy.get(store, other_album(store, 'Another release'), 200)[1]
        assert deferred == {'status': 'daily_limit', 'retry_at': 86400}
        assert copy.settings(store, 200)['requests_today'] == 1
        # A sent request with an ambiguous outcome must never be rebilled.
        assert copy.get(store, ALBUM, 86401)[1]['status'] == 'uncertain'
        assert copy.settings(store, 86401)['requests_today'] == 0
        assert copy.get(store, other_album(store, 'Another release'), 86401)[1]['status'] == 'unavailable'
        assert copy.settings(store, 86401)['requests_today'] == 1
    finally:
        store.conn.close()


def test_atomic_lease_prevents_overlapping_workers_and_expired_claim_cannot_erase_success(store, generated):
    owner = store.album_descriptions
    album_id = owner.album_id(ALBUM)
    other_id = owner.album_id(other_album(store, 'Other release'))
    policy = deepseek.description_policy()
    claim = owner.claim(album_id, policy, 'key-id', 20, 100)
    assert claim['status'] == 'reserved'
    assert owner.claim(album_id, policy, 'key-id', 20, 101)['status'] == 'busy'
    assert owner.claim(other_id, policy, 'key-id', 20, 101)['status'] == 'busy'
    new_claim = owner.claim(album_id, policy, 'key-id', 20, 161)
    good = {'description': 'Saved copy.', 'model': deepseek.MODEL, 'status': 'ready'}
    assert owner.authorize(new_claim['request_id'], new_claim['token'], 'key-id', 20, 162)
    assert owner.finish(new_claim['request_id'], new_claim['token'], good, 162)
    assert not owner.finish(claim['request_id'], claim['token'],
                            {'status': 'unavailable', 'retry_at': 2000}, 163)
    assert copy.read(store, ALBUM)['text'] == 'Saved copy.'
    assert copy.settings(store, 164)['requests_today'] == 2


def test_two_database_connections_share_the_reservation(file_backed_store):
    first = file_backed_store
    second = Store(first._db_path)
    try:
        second.init_schema()
        own_album(first)
        album_id = first.album_descriptions.album_id(ALBUM)
        first.album_descriptions.claim(album_id, deepseek.description_policy(), 'key-id', 20, 100)
        assert second.album_descriptions.claim(album_id, deepseek.description_policy(),
                                               'key-id', 20, 101)['status'] == 'busy'
        assert second.album_descriptions.requests_today(101) == 1
    finally:
        second.conn.close()


def test_reservation_uses_current_clock_after_earlier_worker_work(store, generated):
    copy.get(store, ALBUM, 1, now_fn=lambda: 10000 + (2 if generated else 0))
    owner = store.album_descriptions
    record = owner.cached(owner.album_id(ALBUM), owner.policy_version(deepseek.description_policy()))
    assert owner.requests_today(10002) == 1 and record['at'] == 10002


def test_auth_failure_stops_all_albums_until_key_changes(store, generated, monkeypatch):
    def rejected(album, send):
        generated.append('rejected')
        send({'controlled_album': album['browse_id']})
        raise deepseek.DescriptionUnavailable('auth')
    monkeypatch.setattr(deepseek, 'describe', rejected)
    assert copy.get(store, ALBUM, 100)[1]['status'] == 'auth'
    assert copy.get(store, other_album(store, 'Another album'), 200000)[1]['status'] == 'auth'
    assert len(generated) == 1
    monkeypatch.setattr(deepseek._iao, 'api_key', lambda *args: 'a-fixed-key')
    assert copy.get(store, ALBUM, 102)[1]['status'] == 'auth'
    assert len(generated) == 2  # A new credential can retry the same failed record immediately.


@pytest.mark.parametrize('reason,delay', [('rate_limit', 3600), ('balance', 86400), ('unavailable', 900)])
def test_provider_failure_backs_off_across_different_albums(store, generated, monkeypatch, reason, delay):
    def fail(album, send):
        generated.append(reason)
        send({'controlled_album': album['browse_id']})
        raise deepseek.DescriptionUnavailable(reason)
    monkeypatch.setattr(deepseek, 'describe', fail)
    copy.get(store, ALBUM, 100)
    assert copy.get(store, other_album(store, 'Other record'), 100 + delay - 1)[1]['status'] == reason
    assert len(generated) == 1
    if reason == 'unavailable':
        assert copy.get(store, ALBUM, 100 + delay)[1]['status'] == 'uncertain'
        assert len(generated) == 1
        copy.get(store, other_album(store, 'Other record'), 100 + delay)
    else:
        copy.get(store, ALBUM, 100 + delay)
    assert len(generated) == 2


def test_unknown_record_has_negative_cache_without_blocking_other_records(store, generated, monkeypatch):
    def unknown(album, send):
        generated.append('unknown')
        send({'controlled_album': album['browse_id']})
        return {'description': None, 'model': deepseek.MODEL, 'confidence': 'low', 'usage': {}}
    monkeypatch.setattr(deepseek, 'describe', unknown)
    assert copy.get(store, ALBUM, 100)[0] is None
    assert copy.get(store, ALBUM, 101)[1]['retry_at'] == 100 + 7 * 86400
    assert len(generated) == 1
    copy.get(store, other_album(store, 'Other release'), 102)
    assert len(generated) == 2


def test_disabled_missing_key_or_zero_limit_never_requests_provider(store, generated, monkeypatch):
    store.settings.set_setting('deepseek_album_descriptions_enabled', '0')
    assert copy.get(store, ALBUM, 100)[1]['status'] == 'paused'
    store.settings.set_setting('deepseek_album_descriptions_enabled', '1')
    store.settings.set_setting('deepseek_daily_limit', '0')
    assert copy.get(store, ALBUM, 100)[1]['status'] == 'paused'
    monkeypatch.setattr(deepseek._iao, 'api_key', lambda *args: None)
    assert copy.get(store, ALBUM, 100)[1]['status'] == 'not_configured'
    assert generated == []
    assert store.album_descriptions.requests_today(100) == 0
    assert not store.conn.execute('SELECT 1 FROM album_description_requests').fetchone()


def test_usage_and_generated_records_do_not_contain_credentials(store, generated):
    copy.get(store, ALBUM, 100)
    from yt_playlist.core import album_description_files, fao
    rows = [tuple(row) for row in store.conn.execute('SELECT * FROM album_description_requests')]
    files = [fao.read(path).bytes.decode('utf-8')
             for path in album_description_files.directory(store.conn).glob('*/*.json')]
    assert rows and files
    assert 'secret-key' not in json.dumps(rows + files)
    assert not store.conn.execute("SELECT 1 FROM rec_proposals WHERE surface LIKE 'album_description%'").fetchone()


def test_worker_prepares_refreshed_menu_instead_of_buying_copy_for_previous_menu(store, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home
    from yt_playlist.rec import album_choices, album_notes, home_prepared
    from yt_playlist.rec.worker_tasks import ServingTasks
    from tests.test_growth_journey import early_journey
    early_journey(store)
    # Keep the real Home routes, template globals and shutdown ownership.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx) for module in
                                                        (home, app_update, audio_preview)])
    app = webapp.create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    albums = [{'browse_id': f'record-{i}', 'artist': f'Artist {i}', 'title': f'Album {i}',
               'score': 1 - i / 20, 'classification': 'discovery'} for i in range(12)]
    payload = {'items': albums, 'context': {}, 'built_at': 1}
    store.rec_proposals.put_proposals('album_prepared', payload, 1)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.get('/home/album-choices').status_code == 200
        initial, = store.rec_ledger.evidence().records('opportunity')
        response = client.post('/home/album-choices/refresh', data={'opportunity_id': initial['id']})
        assert response.status_code == 200
        offers = store.rec_ledger.evidence().records('opportunity')
        current = next(offer for offer in offers if offer['id'] != initial['id'])
        expected = {option['browse_id'] for option in current['options']}
        assert expected.isdisjoint({option['browse_id'] for option in initial['options']})
    assert not app.state.ctx.work.active
    prepared = []
    def stage(name, target, *args, **kwargs):
        if target is album_choices.prepare_cached:
            return payload
        if target is home_prepared.prepare:
            return {'menus': [1], 'modes': [], 'play_exposure': []}
        if target is album_notes.prepare:
            prepared.extend(album['browse_id'] for album in args[2])
        return None
    ServingTasks(SimpleNamespace(store=store, now_fn=lambda: 1, client_provider=lambda: {}), stage).home()
    assert set(prepared) == expected
