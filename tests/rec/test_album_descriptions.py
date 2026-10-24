import json

import pytest

from yt_playlist.core.store import Store
from yt_playlist.providers import deepseek
from yt_playlist.rec import album_descriptions as copy

ALBUM = {'browse_id': 'first-id', 'artist': 'Patrice Bäumel',
         'title': 'Global Underground #42: Berlin (DJ Mix)', 'year': '2019'}


@pytest.fixture
def generated(store, monkeypatch):
    monkeypatch.setattr(deepseek, 'api_key', lambda *args: 'secret-key')
    calls = []
    def describe(album, key):
        calls.append((album, key))
        return {'description': 'Persistent album copy.', 'confidence': 'moderate', 'uncertainty': '',
                'model': deepseek.MODEL, 'prompt_version': 1, 'usage': {'total_tokens': 319}}
    monkeypatch.setattr(deepseek, 'describe', describe)
    return calls


def test_refresh_alternate_id_disable_and_key_change_reuse_paid_copy(store, generated, monkeypatch):
    description, record = copy.get(store, ALBUM, 100)
    assert description['kind'] == 'generated' and not description['quote']
    assert record['usage']['total_tokens'] == 319
    other = {**ALBUM, 'browse_id': 'another-id', 'artist': 'Patrice Baumel', 'year': '2023'}
    assert copy.get(store, other, 200)[0] == description
    store.settings.set_setting('deepseek_album_descriptions_enabled', '0')
    monkeypatch.setattr(deepseek, 'api_key', lambda *args: 'replacement-secret')
    assert copy.get(store, other, 999999)[0] == description
    assert copy.read(store, other) == description
    assert len(generated) == 1
    assert copy.identity(ALBUM) != copy.identity({**ALBUM, 'title': ALBUM['title'].replace('DJ Mix', 'Unmixed')})


def test_durable_copy_survives_database_reopen_and_home_cache_eviction(tmp_path, generated):
    path = tmp_path / 'state.db'
    first = Store(path)
    first.init_schema()
    description = copy.get(first, ALBUM, 100)[0]
    for number in range(130):
        first.rec_proposals.publish_album_note(str(number), {'description': None}, 200)
    first.conn.close()
    second = Store(path)
    second.init_schema()
    try:
        assert copy.get(second, ALBUM, 99999999)[0] == description
        assert len(generated) == 1
    finally:
        second.conn.close()


def test_daily_limit_counts_failures_survives_restart_and_does_not_reset_on_new_key(tmp_path, generated, monkeypatch):
    path = tmp_path / 'state.db'
    store = Store(path)
    store.init_schema()
    store.settings.set_setting('deepseek_daily_limit', '1')
    def fail(*args):
        raise deepseek.DescriptionUnavailable('unavailable')
    monkeypatch.setattr(deepseek, 'describe', fail)
    assert copy.get(store, ALBUM, 100)[1]['status'] == 'unavailable'
    store.conn.close()
    store = Store(path)
    store.init_schema()
    try:
        monkeypatch.setattr(deepseek, 'api_key', lambda *args: 'another-key')
        store.rec_proposals.wake_album_descriptions(200)
        deferred = copy.get(store, {**ALBUM, 'title': 'Another release'}, 200)[1]
        assert deferred == {'status': 'daily_limit', 'retry_at': 86400}
        assert copy.settings(store, 200)['requests_today'] == 1
        assert copy.get(store, ALBUM, 86401)[1]['status'] == 'unavailable'
        assert copy.settings(store, 86401)['requests_today'] == 1
    finally:
        store.conn.close()


def test_atomic_lease_prevents_overlapping_workers_and_expired_claim_cannot_erase_success(store, generated):
    claim = store.rec_proposals.claim_album_description(copy.identity(ALBUM), 'key-id', 20, 100)
    assert claim['status'] == 'reserved'
    assert store.rec_proposals.claim_album_description(copy.identity(ALBUM), 'key-id', 20, 101)['status'] == 'busy'
    assert store.rec_proposals.claim_album_description('other-record', 'key-id', 20, 101)['status'] == 'busy'
    new_claim = store.rec_proposals.claim_album_description(copy.identity(ALBUM), 'key-id', 20, 161)
    good = {'description': 'Saved copy.', 'model': deepseek.MODEL, 'status': 'ready'}
    store.rec_proposals.finish_album_description(copy.identity(ALBUM), new_claim['token'], good, 162)
    store.rec_proposals.finish_album_description(copy.identity(ALBUM), claim['token'],
                                                {'status': 'unavailable', 'retry_at': 2000}, 163)
    assert copy.read(store, ALBUM)['text'] == 'Saved copy.'
    assert copy.settings(store, 164)['requests_today'] == 2


def test_two_database_connections_share_the_reservation(tmp_path):
    path = tmp_path / 'state.db'
    first, second = Store(path), Store(path)
    first.init_schema()
    second.init_schema()
    try:
        first.rec_proposals.claim_album_description('record', 'key-id', 20, 100)
        assert second.rec_proposals.claim_album_description('record', 'key-id', 20, 101)['status'] == 'busy'
        assert second.rec_proposals.get_proposals('album_description_usage')['requests'] == 1
    finally:
        first.conn.close()
        second.conn.close()


def test_reservation_uses_current_clock_after_earlier_worker_work(store, generated):
    clock = iter([10000, 10002])
    copy.get(store, ALBUM, 1, now_fn=lambda: next(clock))
    usage = store.rec_proposals.get_proposals('album_description_usage')
    record = store.rec_proposals.get_proposals('album_description:' + copy.identity(ALBUM))
    assert usage['requests'] == 1 and record['at'] == 10002


def test_auth_failure_stops_all_albums_until_key_changes(store, generated, monkeypatch):
    def rejected(*args):
        generated.append('rejected')
        raise deepseek.DescriptionUnavailable('auth')
    monkeypatch.setattr(deepseek, 'describe', rejected)
    assert copy.get(store, ALBUM, 100)[1]['status'] == 'auth'
    assert copy.get(store, {**ALBUM, 'title': 'Another album'}, 200000)[1]['status'] == 'auth'
    assert len(generated) == 1
    monkeypatch.setattr(deepseek, 'api_key', lambda *args: 'a-fixed-key')
    assert copy.get(store, ALBUM, 102)[1]['status'] == 'auth'
    assert len(generated) == 2  # A new credential can retry the same failed record immediately.


@pytest.mark.parametrize('reason,delay', [('rate_limit', 3600), ('balance', 86400), ('unavailable', 900)])
def test_provider_failure_backs_off_across_different_albums(store, generated, monkeypatch, reason, delay):
    def fail(*args):
        generated.append(reason)
        raise deepseek.DescriptionUnavailable(reason)
    monkeypatch.setattr(deepseek, 'describe', fail)
    copy.get(store, ALBUM, 100)
    assert copy.get(store, {**ALBUM, 'title': 'Other record'}, 100 + delay - 1)[1]['status'] == reason
    assert len(generated) == 1
    copy.get(store, ALBUM, 100 + delay)
    assert len(generated) == 2


def test_unknown_record_has_negative_cache_without_blocking_other_records(store, generated, monkeypatch):
    def unknown(*args):
        generated.append('unknown')
        return {'description': None, 'model': deepseek.MODEL, 'confidence': 'low', 'usage': {}}
    monkeypatch.setattr(deepseek, 'describe', unknown)
    assert copy.get(store, ALBUM, 100)[0] is None
    assert copy.get(store, ALBUM, 101)[1]['retry_at'] == 100 + 7 * 86400
    assert len(generated) == 1
    copy.get(store, {**ALBUM, 'title': 'Other release'}, 102)
    assert len(generated) == 2


def test_disabled_missing_key_or_zero_limit_never_requests_provider(store, generated, monkeypatch):
    store.settings.set_setting('deepseek_album_descriptions_enabled', '0')
    assert copy.get(store, ALBUM, 100)[1]['status'] == 'paused'
    store.settings.set_setting('deepseek_album_descriptions_enabled', '1')
    store.settings.set_setting('deepseek_daily_limit', '0')
    assert copy.get(store, ALBUM, 100)[1]['status'] == 'paused'
    monkeypatch.setattr(deepseek, 'api_key', lambda *args: None)
    assert copy.get(store, ALBUM, 100)[1]['status'] == 'not_configured'
    assert generated == []
    assert store.rec_proposals.get_proposals('album_description_usage') is None


def test_usage_and_generated_records_do_not_contain_credentials(store, generated):
    copy.get(store, ALBUM, 100)
    rows = store.conn.execute("SELECT payload FROM rec_proposals WHERE surface LIKE 'album_description%'").fetchall()
    assert 'secret-key' not in json.dumps([row[0] for row in rows])


def test_worker_prepares_refreshed_menu_instead_of_buying_copy_for_previous_menu(store):
    from types import SimpleNamespace
    from tests.web_support import _client
    from yt_playlist.rec import album_choices, album_notes, home_prepared
    from yt_playlist.rec.worker_tasks import ServingTasks
    client = _client(store, lambda: {})
    albums = [{'browse_id': f'record-{i}', 'artist': f'Artist {i}', 'title': f'Album {i}',
               'score': 1 - i / 20, 'classification': 'discovery'} for i in range(12)]
    payload = {'items': albums, 'context': {}, 'built_at': 1}
    store.rec_proposals.put_proposals('album_prepared', payload, 1)
    client.get('/home/album-choices')
    initial, = store.rec_ledger.evidence().records('opportunity')
    response = client.post('/home/album-choices/refresh', data={'opportunity_id': initial['id']})
    assert response.status_code == 200
    offers = store.rec_ledger.evidence().records('opportunity')
    current = next(offer for offer in offers if offer['id'] != initial['id'])
    expected = {option['browse_id'] for option in current['options']}
    assert expected.isdisjoint({option['browse_id'] for option in initial['options']})
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
