"""Controlled billed-access admission through the real album-copy workflow."""
import io
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from tests.providers.test_deepseek import ALBUM, TEXT, response
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.integrations import deepseek as transport
from yt_playlist.providers import deepseek
from yt_playlist.rec import album_descriptions as copy
from yt_playlist.util import net

KEY = 'sk-SECRET-0123456789'


@pytest.fixture
def boundary(store, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    store.settings.set_setting('deepseek_api_key', KEY)

    def install(fetch=lambda request: response(), pace_fn=None):
        clock, calls, responses, events = [1000.0], [], [], []

        def pace():
            assert not store._lock.held_by_current_thread and not store.conn.in_transaction
            events.append('pace')
            if pace_fn:
                pace_fn()

        def open_request(request, *, timeout):
            assert not store._lock.held_by_current_thread and not store.conn.in_transaction
            calls.append((request, timeout))
            events.append('request')
            body = fetch(request)
            result = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
            responses.append(result)
            return result

        iao = transport.DeepSeekIAO(now_fn=lambda: clock[0], open_fn=open_request,
                                    pacer=SimpleNamespace(wait=pace))
        monkeypatch.setattr(deepseek, '_iao', iao)
        return SimpleNamespace(iao=iao, clock=clock, calls=calls, responses=responses, events=events)
    return install


def get(b, store, album=ALBUM):
    return copy.get(store, album, b.clock[0], now_fn=lambda: b.clock[0])


def test_exact_post_is_reserved_once_and_only_public_album_identity_is_sent(boundary, store):
    def fetch(request):
        usage = store.rec_proposals.get_proposals('album_description_usage')
        assert usage['requests'] == 1 and usage['status'] == 'pending' and usage['lease_until'] == 1060
        return response()

    b = boundary(fetch)
    description, record = get(b, store)
    assert description['text'] == TEXT and record['usage']['total_tokens'] == 319
    assert get(b, store, {**ALBUM, 'browse_id': 'different-local-id'})[0] == description
    assert len(b.calls) == 1 and b.events == ['pace', 'request'] and b.responses[0].closed
    request, timeout = b.calls[0]
    assert request.full_url == transport.ENDPOINT and request.get_method() == 'POST' and timeout == 20
    assert request.get_header('Authorization') == 'Bearer ' + KEY
    assert request.get_header('Content-type') == 'application/json'
    payload = json.loads(request.data)
    assert payload['model'] == 'deepseek-flash' and payload['max_tokens'] == 400
    assert payload['thinking'] == {'type': 'disabled'}
    assert json.loads(payload['messages'][1]['content']) == {
        'artist': ALBUM['artist'], 'title': ALBUM['title'], 'year': ALBUM['year']}
    assert not any(private in request.data.decode() for private in ('private-local-id', 'taste', '99', KEY))
    assert transport.DeepSeekIAO().pacer.min_interval == 1
    rows = store.conn.execute("SELECT surface,payload FROM rec_proposals").fetchall()
    assert KEY not in json.dumps([tuple(row) for row in rows])


def test_concurrent_albums_share_the_reservation_without_holding_database_lock(boundary, store):
    entered, release = Event(), Event()

    def fetch(request):
        entered.set()
        assert release.wait(5), 'Test did not release DeepSeek request'
        return response()

    b = boundary(fetch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        active = pool.submit(get, b, store)
        try:
            assert entered.wait(3)
            others = [pool.submit(get, b, store, {**ALBUM, 'title': f'Other {index}'}) for index in range(6)]
            assert all(future.result(2)[1]['status'] == 'busy' for future in others)
            pool.submit(store.settings.set_setting, 'foreground', 'saved').result(2)
            assert store.settings.get_setting('foreground') == 'saved'
        finally:
            release.set()
        assert active.result(3)[0]['text'] == TEXT
    assert len(b.calls) == 1 and copy.settings(store, 1000)['requests_today'] == 1


@pytest.mark.parametrize('code,headers,status,deadline', [
    (401, {}, 'auth', 87400), (403, {}, 'auth', 87400), (402, {}, 'balance', 87400),
    (429, {'Retry-After': '7200'}, 'rate_limit', 8200),
    (503, {'Retry-After': 'Thu, 01 Jan 1970 00:50:00 GMT'}, 'unavailable', 3000),
    (503, {'Retry-After': 'NaN'}, 'unavailable', 1900),
    (429, {'Retry-After': 'inf'}, 'rate_limit', 4600),
])
def test_failures_are_safe_durable_and_shared_without_billed_retry(boundary, store, code, headers, status, deadline):
    body = io.BytesIO(b'SECRET upstream body')

    def fetch(request):
        raise urllib.error.HTTPError(request.full_url, code, KEY, headers, body)

    b = boundary(fetch)
    with net.probe_errors() as errors:
        assert get(b, store)[1] == {'status': status, 'at': 1000, 'retry_at': deadline}
        assert get(b, store, {**ALBUM, 'title': 'Other record'})[1]['status'] == status
    assert len(b.calls) == 1 and body.closed and len(errors) == 1
    assert errors[0].provider == 'deepseek' and errors[0].retry_at == deadline
    store.rec_proposals.wake_album_descriptions(1000)
    b = boundary(lambda request: pytest.fail('Fresh IAO bypassed durable failure'))
    assert get(b, store)[1]['retry_at'] == deadline and not b.calls
    assert copy.settings(store, 1000)['requests_today'] == 1
    rows = store.conn.execute("SELECT surface,payload FROM rec_proposals").fetchall()
    assert KEY not in json.dumps([tuple(row) for row in rows])


def test_rejected_credential_remains_rejected_after_replacement_and_restart(boundary, tmp_path):
    path = tmp_path / 'deepseek.db'
    db = Store(path)
    db.init_schema()
    db.settings.set_setting('deepseek_api_key', KEY)

    def fetch(request):
        if request.get_header('Authorization') == 'Bearer ' + KEY:
            raise urllib.error.HTTPError(request.full_url, 401, 'no', {}, io.BytesIO())
        return response()

    b = boundary(fetch)
    try:
        assert get(b, db)[1]['status'] == 'auth'
        db.settings.set_setting('deepseek_api_key', 'sk-replacement-12345678')
        db.rec_proposals.wake_album_descriptions(1000)
        assert get(b, db)[0]['text'] == TEXT
        assert copy.settings(db, 1000)['requests_today'] == 2
    finally:
        db.conn.close()
    db = Store(path)
    db.init_schema()
    try:
        db.settings.set_setting('deepseek_api_key', KEY)
        b = boundary(lambda request: pytest.fail('Old rejected credential was admitted'))
        b.clock[0] = 200000  # Authentication failures do not time out into paid retries.
        assert get(b, db, {**ALBUM, 'title': 'Other record'})[1]['status'] == 'auth'
        assert not b.calls
        assert get(b, db)[0]['text'] == TEXT  # Existing paid prose survives any access state.
    finally:
        db.conn.close()


def test_two_connections_share_daily_cap_even_after_key_change(boundary, tmp_path):
    path = tmp_path / 'budget.db'
    first, second = Store(path), Store(path)
    first.init_schema()
    second.init_schema()
    first.settings.set_values({'deepseek_api_key': KEY, 'deepseek_daily_limit': '1'})
    b = boundary()
    try:
        assert get(b, first)[0]['text'] == TEXT
        second.settings.set_setting('deepseek_api_key', 'sk-new-12345678')
        assert get(b, second, {**ALBUM, 'title': 'Other record'})[1] == {
            'status': 'daily_limit', 'retry_at': 86400}
        assert len(b.calls) == 1
        b.clock[0] = 86400
        assert get(b, second, {**ALBUM, 'title': 'Other record'})[0]['text'] == TEXT
        assert len(b.calls) == 2 and copy.settings(first, 86400)['requests_today'] == 1
    finally:
        first.conn.close()
        second.conn.close()


def test_midnight_during_pacing_defers_old_day_admission_without_a_post(boundary, store):
    store.settings.set_setting('deepseek_daily_limit', '1')
    def pace():
        b.clock[0] = max(b.clock[0], 86401)
    b = boundary(pace_fn=pace)
    b.clock[0] = 86399
    assert get(b, store)[1]['status'] == 'paused'
    assert not b.calls
    previous = store.rec_proposals.get_proposals('album_description_usage')
    assert previous['day'] == 0 and previous['requests'] == 1
    assert previous['send_authorized'] is False and previous['lease_until'] == 0
    assert get(b, store, {**ALBUM, 'title': 'Another release'})[0]['text'] == TEXT
    assert get(b, store, {**ALBUM, 'title': 'Third release'})[1] == {
        'status': 'daily_limit', 'retry_at': 172800}
    assert len(b.calls) == 1
    usage = store.rec_proposals.get_proposals('album_description_usage')
    assert usage['day'] == 1 and usage['requests'] == 1 and usage['send_authorized'] is True


def test_expired_lease_during_pacing_cannot_authorize_a_post(boundary, store):
    def pace():
        b.clock[0] += 60
    b = boundary(pace_fn=pace)
    assert get(b, store)[1]['status'] == 'paused'
    usage = store.rec_proposals.get_proposals('album_description_usage')
    assert not b.calls and usage['requests'] == 1 and usage['send_authorized'] is False
    assert usage['token'] is None and usage['lease_until'] == 0


def test_lease_takeover_during_pacing_keeps_new_owner_and_authorizes_only_once(boundary, tmp_path):
    path = tmp_path / 'lease-takeover.db'
    first, second = Store(path), Store(path)
    first.init_schema()
    second.init_schema()
    first.settings.set_setting('deepseek_api_key', KEY)
    replacement = {}
    def pace():
        b.clock[0] += 61
        replacement.update(second.rec_proposals.claim_album_description(
            'another-reservation', transport.credential_id(KEY), 20, b.clock[0]))
    b = boundary(pace_fn=pace)
    try:
        assert get(b, first)[1]['status'] == 'paused' and not b.calls
        assert replacement['status'] == 'reserved'
        usage = first.rec_proposals.get_proposals('album_description_usage')
        assert usage['token'] == replacement['token'] and usage['requests'] == 2
        assert usage['status'] == 'pending' and usage['send_authorized'] is False
        assert not first.rec_proposals.get_proposals('album_description:' + copy.identity(ALBUM))
        assert first.rec_proposals.authorize_album_description_send(
            replacement['token'], transport.credential_id(KEY), 20, b.clock[0])
        assert not second.rec_proposals.authorize_album_description_send(
            replacement['token'], transport.credential_id(KEY), 20, b.clock[0])
        assert first.rec_proposals.get_proposals('album_description_usage')['requests'] == 2
    finally:
        first.conn.close()
        second.conn.close()


def test_authorization_renews_lease_for_transport_after_a_long_pacing_wait(boundary, store):
    def pace():
        b.clock[0] += 55
    def fetch(request):
        usage = store.rec_proposals.get_proposals('album_description_usage')
        assert usage['send_authorized'] is True and usage['lease_until'] == 1115
        assert store.rec_proposals.claim_album_description(
            'overlapping-attempt', transport.credential_id(KEY), 20, 1061)['status'] == 'busy'
        return response()
    b = boundary(fetch, pace_fn=pace)
    assert get(b, store)[0]['text'] == TEXT and len(b.calls) == 1


@pytest.mark.parametrize('bad', [b'invalid json', b' ' * (transport.MAX_RESPONSE_BYTES + 1), {},
                                 response(description='Too short.')])
def test_invalid_response_defers_only_the_record_and_closes_response(boundary, store, bad):
    b = boundary(lambda request: bad if len(b.calls) == 1 else response())
    assert get(b, store)[1]['status'] == 'invalid_response'
    assert get(b, store)[1]['retry_at'] == 87400
    assert len(b.calls) == 1 and b.responses[0].closed
    assert get(b, store, {**ALBUM, 'title': 'Other record'})[0]['text'] == TEXT
    assert len(b.calls) == 2 and copy.settings(store, 1000)['requests_today'] == 2


def test_timeout_recovers_once_after_deadline_and_preserves_saved_copy_when_disabled(boundary, store):
    def fetch(request):
        if len(b.calls) == 1:
            raise TimeoutError('controlled timeout')
        return response()

    b = boundary(fetch)
    assert get(b, store)[1]['retry_at'] == 1900
    b.clock[0] = 1899
    assert get(b, store)[1]['status'] == 'unavailable' and len(b.calls) == 1
    b.clock[0] = 1900
    assert get(b, store)[0]['text'] == TEXT and len(b.calls) == 2
    store.settings.set_values({'deepseek_album_descriptions_enabled': '0', 'deepseek_api_key': ''})
    assert get(b, store)[0]['text'] == TEXT and len(b.calls) == 2


@pytest.mark.parametrize('change', ['disable', 'key', 'limit'])
def test_changed_admission_during_pacing_never_sends_a_paid_post(boundary, store, change):
    def pace():
        values = {'disable': {'deepseek_album_descriptions_enabled': '0'},
                  'key': {'deepseek_api_key': 'sk-replacement-12345678'},
                  'limit': {'deepseek_daily_limit': '0'}}[change]
        store.settings.set_values(values)

    b = boundary(lambda request: pytest.fail('Changed admission sent a billed POST'), pace_fn=pace)
    assert get(b, store)[1]['status'] == 'paused'
    assert not b.calls
    assert store.rec_proposals.get_proposals('album_description_usage')['requests'] == 1
    assert not store.rec_proposals.get_proposals('deepseek_host')


@pytest.mark.parametrize('error_type', [WorkClosing, WorkBudgetExceeded])
def test_cancelled_post_stays_counted_and_is_not_replayed(boundary, store, error_type):
    def fetch(request):
        raise error_type('cancelled')

    b = boundary(fetch)
    with pytest.raises(error_type):
        get(b, store)
    assert len(b.calls) == 1
    usage = store.rec_proposals.get_proposals('album_description_usage')
    assert usage['requests'] == 1 and usage['lease_until'] == 1060 and usage['status'] == 'pending'
    assert get(b, store)[1]['status'] == 'busy' and len(b.calls) == 1


def test_an_admission_send_function_cannot_be_reused_after_publication(boundary, store):
    b = boundary()
    send_functions = []

    def project(send):
        send_functions.append(send)
        result = deepseek.describe(ALBUM, send)
        with pytest.raises(RuntimeError, match='exactly one request'):
            send({})
        return {**result, 'status': 'ready', 'at': 1000}

    b.iao.complete(store, 'controlled-identity', project, 1000)
    with pytest.raises(RuntimeError, match='exactly one request'):
        send_functions[0]({})
    assert len(b.calls) == 1 and copy.settings(store, 1000)['requests_today'] == 1
