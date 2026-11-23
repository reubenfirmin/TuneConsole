"""Confirmed playback controls retain local evidence while maintenance owns SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import without_metadata_providers
from tests.playback_feedback_support import paused_recording
from tests.store_fixture_support import _file_consumer_store, bounded_consumer_schema, initialize_consumer, without_query_timing
from yt_playlist.core import fao, playback_feedback
from yt_playlist.core.store import Store
from yt_playlist.library import live_plays, player_events, ratings
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.bridge import _now_playing_snapshot


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def feedback_routes(monkeypatch, without_metadata_providers):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_vibes, discovery, likes, sync, track

    # Preserve the actual bridge, maintenance middleware and every requested
    # route. Unrelated pages and idle shutdown are below the feedback boundary.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx) for module in
                        (app_update, audio_vibes, discovery, likes, sync, track)])
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)


@pytest.fixture(scope='module')
def _pristine_current_schema():
    """Playback/inbox owners consume real private SQL without genre preparation."""
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture
def file_backed_store(tmp_path, _pristine_current_schema):
    # Keep instrumented SQL, independent readers, WAL and real revision guards:
    # these cases deliberately hold writers and replace/reopen the database.
    with _file_consumer_store(tmp_path, _pristine_current_schema) as store:
        yield store


@pytest.fixture
def library(file_backed_store):
    store = file_backed_store
    main = store.accounts.upsert_account('Main', None, True)
    other = store.accounts.upsert_account('Other', 'other', False)
    entry = store.tracks.upsert_track('known', 'Song', 'Artist', None, 180)
    lm = store.playlists.upsert_playlist(main, 'LM', 'Liked Music', 0, '', 1)
    return store, main, other, entry, lm


def playing(video='known'):
    return _now_playing_snapshot({'videoId': video, 'title': 'Song', 'artist': 'Artist',
        'playlist': 'PLplaying', 'position': 15, 'duration': 180, 'brandKnown': True, 'brandId': ''})


def test_late_confirmed_like_uses_the_original_verified_choice(library):
    from yt_playlist.library.choice_composition import playback
    from yt_playlist.rec import choice_attribution, choice_evidence

    store, main, _, _, _ = library
    first, later = [choice_evidence.issue(store, 'mixes', [{'id': 'song'}], at, context={})
                    for at in (800, 950)]
    for offer, at, action in ((first, 810, 'played-choice'), (later, 960, 'later-choice')):
        choice_evidence.respond(store, offer, 'choice', at, option_id='song', action_id=action)
    playback(store, synchronous=True).started(main, 'PLplaying', 'known', first, 'song', 820,
        ids=('verified-start', 'verified-experience'), details={'choice_action_id': 'played-choice'})
    remote = FakeClient()
    app = create_app(store, lambda: {main: remote}, now_fn=lambda: 1000, background_work=False)
    bridge = app.state.ctx.bridge

    async def confirm_native(frame, timeout=10):
        assert frame['type'] == 'rate'
        return 200, '{"ok":true}'

    bridge._send = lambda _: None
    bridge.request_control = confirm_native
    bridge.now_playing = playing()
    bridge.now_playing_seen_at = time.monotonic()
    client = TestClient(app, base_url='http://127.0.0.1')
    response = client.post('/now-playing/rate', json={'action': 'like', 'videoId': 'known', 'likeStatus': 'LIKE'})
    assert response.json() == {'ok': True, 'error': ''}
    assert remote.rated == [('known', 'LIKE')]
    assert store.tracks.item_rating(main, 'known') == {'status': 'LIKE', 'source': 'api', 'observed_at': 1000}
    assert tuple(store.conn.execute(
        "SELECT account_id,video_id,playlist_ytm_id,at FROM player_events WHERE kind='confirmed_rating'"
    ).fetchone()) == (main, 'known', 'PLplaying', 1000)

    ledger = store.rec_ledger.evidence()
    responses = choice_attribution.observed_responses(ledger.records('response'), 1001)
    decisions = choice_attribution.actions({offer: ledger.get('opportunity', offer) for offer in (first, later)},
                                          responses, 700)
    outcomes = {decision['choice']['id']: [row['kind'] for row in decision['outcomes']] for decision in decisions}
    assert outcomes == {'played-choice': ['start', 'experience', 'save'], 'later-choice': []}


@pytest.mark.parametrize('defer', [False, True])
@pytest.mark.parametrize('explicit_playlist', [False, True])
def test_native_like_keeps_its_requested_playback_through_confirmation_and_replay(
    library, monkeypatch, defer, explicit_playlist,
):
    from yt_playlist.library.choice_composition import playback
    from yt_playlist.rec import choice_evidence

    store, main, _, _, _ = library
    choices = playback(store, synchronous=True)
    first, second = [choice_evidence.issue(store, 'mixes', [{'id': 'song'}], at - 20, context={})
                     for at in (820, 960)]
    for offer, start, choice in ((first, 820, 'first'), (second, 960, 'second')):
        choice_evidence.respond(store, offer, 'choice', start - 10, option_id='song', action_id=choice)
        choices.started(main, 'PLplaying', 'known', offer, 'song', start,
                        ids=(f'{choice}-start', f'{choice}-experience'), details={'choice_action_id': choice})
    target = {'videoId': 'known'}
    if explicit_playlist:
        target['playlistId'] = 'PLplaying'
    frame = dict(kind='rate', brandKnown=True, brandId='', requestedAt=900000,
        url='https://music.youtube.com/youtubei/v1/like/like', phase='settled', success=True, httpStatus=200,
        href='https://music.youtube.com/watch?v=known&list=PLplaying', body=json.dumps({'target': target}))
    with paused_recording(store) if defer else nullcontext():
        assert player_events.handle_player_event(SimpleNamespace(store=store), frame, 1000)
    if defer:
        def interrupted_ack(_):
            raise InterruptedError('playback acknowledgement interrupted')

        with monkeypatch.context() as interrupted:
            interrupted.setattr(store.playback_feedback, 'acknowledge_observation', interrupted_ack)
            with pytest.raises(InterruptedError, match='acknowledgement interrupted'):
                playback_feedback.drain(store)
        assert playback_feedback.drain(store) == 1

    # The web player's mirror of an already confirmed rating adds no save to a newer choice.
    assert player_events.handle_player_event(SimpleNamespace(store=store), {**frame, 'requestedAt': 1001000}, 1002)
    assert [tuple(row) for row in store.conn.execute(
        "SELECT account_id,video_id,playlist_ytm_id,at FROM player_events WHERE kind='confirmed_rating'"
    )] == [(main, 'known', 'PLplaying', 1000)]
    saves = [row for row in store.rec_ledger.evidence().records('response') if row['kind'] == 'save']
    assert [(row['opportunity_id'], row['details']['choice_action_id'], row['at']) for row in saves] == [(first, 'first', 1000)]


def test_playback_binding_handoff_clears_when_response_is_selected(library):
    from yt_playlist.core import playback_bindings, worker_settings
    from yt_playlist.rec import choice_evidence

    store, main, _, _, _ = library
    offer = choice_evidence.issue(store, 'mixes', [{'id': 'song'}], 90, context={})
    store.rec_ledger.bind_choice_playback(main, 'PLplaying', 'known', offer, 'song', 100, 3600,
                                          response_id='pending-onset')
    pending = worker_settings.read(store.conn, playback_bindings.KEY)
    assert json.loads(pending)[0]['opportunity_id'] == offer
    assert store.rec_ledger.choice_playback(main, 'PLplaying', 'known', 100, 0)['opportunity_id'] == offer
    choice_evidence.respond(store, offer, 'experience', 100, option_id='song', action_id='pending-onset',
        details={'account_id': main, 'playlist': 'PLplaying', 'video_id': 'known'}, synchronous=True)
    assert worker_settings.read(store.conn, playback_bindings.KEY) is None
    receipt = playback_bindings.lookup(store.conn, main, 'PLplaying', 'known', before=100, through=100)
    assert receipt['response_id'] == 'pending-onset'
    assert store.rec_ledger.choice_playback(main, 'PLplaying', 'known', 100, 0)['opportunity_id'] == offer


def test_library_like_toggle_remains_available_with_a_held_writer(library):
    store, main, other, _, lm = library
    remote, wrong_account = FakeClient(), FakeClient()
    app = create_app(store, lambda: {main: remote, other: wrong_account}, now_fn=lambda: 1000,
                     background_work=False)
    store._evidence_recovery_pending = True
    held, release = threading.Event(), threading.Event()

    def hold_writer():
        with store._lock, store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            held.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(hold_writer)
        try:
            assert held.wait(5)
            client = TestClient(app, base_url='http://127.0.0.1')
            for value, pressed in (('1', 'true'), ('', 'false')):
                response = executor.submit(client.post, '/track/like',
                    data={'video_id': 'known', 'on': value}).result(timeout=3)
                assert response.status_code == 200 and f'aria-pressed="{pressed}"' in response.text
            assert remote.rated == [('known', 'LIKE'), ('known', 'INDIFFERENT')]
            assert not wrong_account.rated and playback_feedback.inbox(store).pending() == 2
        finally:
            release.set()
        writer.result(timeout=5)
    store._evidence_recovery_pending = False
    assert playback_feedback.drain(store) == 2
    assert playback_feedback.drain(store) == 0
    assert store.tracks.item_rating(main, 'known')['status'] == 'INDIFFERENT'
    assert store.playlists.get_playlist_track_ids(lm) == []
    assert store.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0] == 2


@pytest.mark.parametrize('account_changed_during_rating', [False, True])
def test_skull_does_not_advance_another_accounts_matching_upload(library, monkeypatch,
                                                               account_changed_during_rating):
    store, main, other, _, _ = library
    remote, wrong_account = FakeClient(), FakeClient()
    app = create_app(store, lambda: {main: remote, other: wrong_account}, now_fn=lambda: 1000,
                     background_work=False)
    ctx, commands = app.state.ctx, []
    ctx.bridge._send = lambda _: None
    ctx.bridge.account_bound_next = True
    ctx.bridge.now_playing = playing()
    if not account_changed_during_rating:
        ctx.bridge.now_playing['brand_id'] = 'other'
    store._evidence_recovery_pending = True

    async def control(frame, timeout=10):
        commands.append(frame)
        ctx.bridge.now_playing['brand_id'] = 'other'
        return 200, '{"ok":false,"error":"Player account is unavailable or different"}'

    monkeypatch.setattr(ctx.bridge, 'request_control', control)
    client = TestClient(app, base_url='http://127.0.0.1')
    response = client.post('/now-playing/rate', json={
        'action': 'dislike', 'videoId': 'known', 'likeStatus': 'DISLIKE'})
    assert response.status_code == 200 and response.json()['ok']
    assert response.json()['player_error'] == 'Rating saved. Some updates are not confirmed; check it in YouTube Music.'
    assert remote.rated == [('known', 'DISLIKE')] and not wrong_account.rated
    assert [frame['type'] for frame in commands] == ['rate']
    assert commands[0]['brandId'] == ''
    assert ratings.main_item_rating(store, main, 'known')['status'] == 'DISLIKE'
    assert ratings.main_item_rating(store, other, 'known') is None


def test_feedback_requires_its_queue_before_replay_or_acknowledgement(library):
    store, main, _, _, _ = library
    queue = playback_feedback.inbox(store)
    playback_feedback.capture(store, 'player', main, {
        'kind': 'next', 'videoId': 'known', 'position': 20, 'duration': 180}, 100)
    path = queue.path
    original = path.read_bytes()
    path.write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        playback_feedback.drain(store)
    assert not store.conn.execute('SELECT 1 FROM playback_feedback_receipts').fetchone()
    assert not store.conn.execute('SELECT 1 FROM player_events').fetchone()
    path.write_bytes(original)
    assert queue.pending() == 1
    assert playback_feedback.drain(store) == 1
    assert not queue.pending() and json.loads(fao.read(path).bytes)['captures'] == []


def test_inbox_reopen_preserves_replay_and_continuing_writes(tmp_path, _pristine_current_schema):
    body = {'kind': 'next', 'videoId': 'known', 'position': 20, 'duration': 180}
    database = tmp_path / 'state.db'
    store = Store(database)
    with closing(store.conn):
        initialize_consumer(store, _pristine_current_schema)
        main = store.accounts.upsert_account('Main', None, True)
        store.tracks.upsert_track('known', 'Song', 'Artist', None, 180)
        capture = playback_feedback.inbox(store).capture('player', main, body, 100)
    store = Store(database)
    with closing(store.conn):
        store.init_schema()
        queue = playback_feedback.inbox(store)
        row = queue.next()
        assert row['sequence'] == 1 and row['capture_id'] == capture
        assert row['account_id'] == main and row['observed_at'] == 100
        assert row['body'] == body
        later = queue.capture('player', main, body, 101)
        assert queue.next()['capture_id'] == capture
        assert playback_feedback.drain(store, limit=1) == 1
        assert store.conn.execute('SELECT capture_id FROM playback_feedback_receipts').fetchone()[0] == capture
        assert store.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 1
        row = queue.next()
        assert row['sequence'] == 2 and row['capture_id'] == later and row['body'] == body
        assert playback_feedback.drain(store) == 1
        assert {row[0] for row in store.conn.execute('SELECT capture_id FROM playback_feedback_receipts')} == {
            capture, later}


def test_rating_skull_and_next_acknowledge_while_writer_is_held_and_survive_restart(library, monkeypatch):
    store, main, other, entry, lm = library
    remote, wrong_account = FakeClient(), FakeClient()
    app = create_app(store, lambda: {main: remote, other: wrong_account}, now_fn=lambda: 1000,
                     background_work=False)
    ctx, commands = app.state.ctx, []
    ctx.bridge._send = lambda _: None
    ctx.bridge.account_bound_next = True
    ctx.bridge.now_playing = playing()
    ctx.bridge.now_playing_seen_at = time.monotonic()
    radio_calls = []

    def forbidden_radio_skip(video_id, *, brand_id=None):
        radio_calls.append((video_id, brand_id))
        pytest.fail('Maintenance must not join Radio preparation')

    monkeypatch.setattr(ctx.radio, 'skip', forbidden_radio_skip)

    async def control(frame, timeout=10):
        commands.append(frame)
        if frame['type'] == 'next':
            ctx.bridge.now_playing = playing('successor')
        return 200, '{"ok":true}'

    monkeypatch.setattr(ctx.bridge, 'request_control', control)
    with paused_recording(store):
        client = TestClient(app, base_url='http://127.0.0.1')
        started = time.monotonic()
        detail = client.get('/track/known')
        assert detail.status_code == 200 and 'Song' in detail.text
        assert time.monotonic() - started < 2
        for action, desired, pending in (('like', 'LIKE', 1), ('like', 'INDIFFERENT', 2),
                                         ('dislike', 'DISLIKE', 4), ('dislike', 'INDIFFERENT', 5)):
            ctx.bridge.now_playing = playing()
            started = time.monotonic()
            response = client.post('/now-playing/rate', json={
                'action': action, 'videoId': 'known', 'likeStatus': desired})
            assert response.status_code == 200 and response.json() == {'ok': True, 'error': ''}
            assert time.monotonic() - started < 2
            assert playback_feedback.inbox(store).pending() == pending
        ctx.bridge.now_playing = playing()
        started = time.monotonic()
        assert client.post('/now-playing/next', json={'videoId': 'known'}).json() == {'ok': True}
        assert time.monotonic() - started < 2
        assert playback_feedback.inbox(store).pending() == 6
        # Exact feedback outside every catalog/playlist context is retained too.
        assert client.post('/now-playing/rate', json={
            'action': 'dislike', 'videoId': 'unseen', 'likeStatus': 'DISLIKE'}).json()['ok']
        status = client.get('/storage-maintenance/status').json()
        assert status['active'] and status['pending_feedback'] == 7
        assert status['capturing_feedback']
        assert client.post('/sync').status_code == 503
        assert ratings.main_rating(store, 'known')['status'] == 'INDIFFERENT'
        assert playback_feedback.drain(store) == 0
        assert not store.conn.execute('SELECT 1 FROM playback_feedback_receipts').fetchone()
    assert radio_calls == []
    assert remote.rated == [('known', status) for status in ('LIKE', 'INDIFFERENT', 'DISLIKE', 'INDIFFERENT')] + [
        ('unseen', 'DISLIKE')]
    assert wrong_account.rated == []
    assert [(frame['videoId'], frame.get('ratingAdvance', False))
            for frame in commands if frame['type'] == 'next'] == [('known', True), ('known', False)]
    assert store.conn.execute('SELECT count(*) FROM item_ratings').fetchone()[0] == 0
    path = store._db_path
    store.conn.close()
    resumed = Store(path)
    try:
        resumed.conn.execute('PRAGMA synchronous=OFF')
        resumed.init_schema()
        assert playback_feedback.drain(resumed) == 7
        assert not playback_feedback.inbox(resumed).pending()
        assert resumed.tracks.item_rating(main, 'known')['status'] == 'INDIFFERENT'
        assert resumed.tracks.item_rating(main, 'unseen')['status'] == 'DISLIKE'
        assert resumed.playlists.get_playlist_track_ids(lm) == []
        rows = list(resumed.conn.execute("SELECT account_id,video_id,position,duration,at FROM player_events WHERE kind='next'"))
        assert list(map(tuple, rows)) == [(main, 'known', 15, 180, 1000)] * 2
        next_events = resumed.player_events.player_events_since(0, kind='next')
        assert [json.loads(event['payload']).get('rating_advance', False) for event in next_events] == [True, False]
        assert resumed.conn.execute('SELECT count(*) FROM confirmed_rating_actions').fetchone()[0] == 5
        # An older complete sync cannot resurrect the old rating, including an
        # omitted action outside fetched playlist contexts.
        ratings.record_observations(resumed, main, {'known': 'LIKE'}, source='sync', now=999)
        ratings.record_observations(resumed, main, {}, source='sync', now=1001)
        assert resumed.tracks.item_rating(main, 'known')['status'] == 'INDIFFERENT'
        ratings.reconcile_pending(resumed, main, SimpleNamespace(get_song=lambda video: {
            'videoDetails': {'videoId': video, 'title': 'Unseen', 'author': 'Artist'}}))
        assert resumed.rec_model.disliked_identity_keys() == {'unseen|artist'}
        assert not resumed.conn.execute('PRAGMA foreign_key_check').fetchone()
        # Replay did not send a third remote Next or another remote rating.
        assert len(commands) == 6
    finally:
        resumed.conn.close()


def test_replay_receipt_survives_crash_after_main_commit_before_inbox_ack(library, monkeypatch):
    store, main, _, _, _ = library
    capture = playback_feedback.capture(store, 'player', main, {
        'kind': 'next', 'videoId': 'known', 'position': 20, 'duration': 180, 'playlist': 'PL'}, 100)
    queue = playback_feedback.inbox(store)

    def interrupted(_):
        raise RuntimeError('interrupted after main commit')

    monkeypatch.setattr(queue, 'acknowledge', interrupted)
    with pytest.raises(RuntimeError, match='after main commit'):
        playback_feedback.drain(store)
    assert store.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 1
    assert store.conn.execute('SELECT capture_id FROM playback_feedback_receipts').fetchone()[0] == capture
    path = store._db_path
    store.conn.close()
    resumed = Store(path)
    try:
        resumed.conn.execute('PRAGMA synchronous=OFF')
        resumed.init_schema()
        assert playback_feedback.drain(resumed) == 1
        assert playback_feedback.drain(resumed) == 0
        assert resumed.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 1
        assert resumed.player_events.skip_blackouts(101) == {'song|artist': 100 + 86400}
    finally:
        resumed.conn.close()


def test_exit_attribution_resumes_after_receipt_commit(library, monkeypatch):
    from yt_playlist.rec import choice_evidence

    store, main, _, _, _ = library
    offer = choice_evidence.issue(store, 'mixes', [{'id': 'chosen'}], 90, context={})
    store.rec_ledger.bind_choice_playback(main, 'PL', 'known', offer, 'chosen', 95, 3600)
    capture = playback_feedback.capture(store, 'player', main, {
        'kind': 'ended', 'videoId': 'known', 'position': 180, 'duration': 180, 'playlist': 'PL'}, 275)

    def interrupted(*_):
        raise RuntimeError('interrupted before exit attribution')

    with monkeypatch.context() as failure:
        failure.setattr(playback_feedback, '_attribute', interrupted)
        with pytest.raises(RuntimeError, match='before exit attribution'):
            playback_feedback.drain(store)

    event, = store.player_events.player_events_since(0, kind='ended')
    assert json.loads(event['payload'])['capture_id'] == capture
    queue = playback_feedback.inbox(store)
    assert queue.pending() == 1

    def before_acknowledgement(_):
        raise RuntimeError('interrupted after exit attribution')

    with monkeypatch.context() as failure:
        failure.setattr(queue, 'acknowledge', before_acknowledgement)
        with pytest.raises(RuntimeError, match='after exit attribution'):
            playback_feedback.drain(store)

    assert queue.pending() == 1
    response, = store.rec_ledger.evidence().records('response')
    event_id = store.player_events.event_for_capture(main, 275, capture)
    assert response['id'] == f'player:{event_id}'
    assert response['opportunity_id'] == offer and response['kind'] == 'experience'
    path = store._db_path
    store.conn.close()
    resumed = Store(path)
    with closing(resumed.conn):
        resumed.init_schema()
        assert playback_feedback.drain(resumed) == 1
        assert playback_feedback.drain(resumed) == 0
        assert resumed.player_events.player_events_since(0, kind='ended') == [event]
        assert resumed.rec_ledger.evidence().records('response') == [response]


@pytest.mark.parametrize('kind', ['player', 'live'])
def test_replay_keeps_capture_while_choice_publication_is_busy(library, kind):
    from yt_playlist.core import group_admission
    from yt_playlist.rec import choice_evidence

    store, main, _, _, _ = library
    if kind == 'player':
        offer = choice_evidence.issue(store, 'mixes', [{'id': 'chosen'}], 90, context={})
        store.rec_ledger.bind_choice_playback(main, 'PL', 'known', offer, 'chosen', 95, 3600)
        body = {'kind': 'ended', 'videoId': 'known', 'position': 180, 'duration': 180, 'playlist': 'PL'}
        relation = 'player_events'
    else:
        offer = choice_evidence.issue(store, 'albums', [{'id': 'album'}], 90, context={})
        store.rec_ledger.remember_album_playback_context({'opportunity_id': offer,
            'browse': 'album', 'playlist': 'PL', 'videos': ['known'], 'at': 95})
        body = {'videoId': 'known', 'title': 'Song', 'artist': 'Artist', 'playlist': 'PL'}
        relation = 'play_events'
    capture = playback_feedback.capture(store, kind, main, body, 275)
    following = playback_feedback.capture(store, 'player', main, {
        'kind': 'ended', 'videoId': 'known', 'playlist': 'unbound', 'position': 180, 'duration': 180}, 276)
    queue = playback_feedback.inbox(store)
    held, release = threading.Event(), threading.Event()

    def hold_publication():
        with group_admission.admission(store):
            held.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as workers:
        publisher = workers.submit(hold_publication)
        try:
            assert held.wait(5)
            assert playback_feedback.drain(store) == 0
            assert queue.pending() == 2 and queue.next()['capture_id'] == capture
            assert store.conn.execute(f'SELECT count(*) FROM {relation}').fetchone()[0] == 1
            assert store.conn.execute('SELECT capture_id FROM playback_feedback_receipts').fetchone()[0] == capture
            assert not store.rec_ledger.evidence().records('response')
        finally:
            release.set()
        publisher.result(timeout=5)

    assert playback_feedback.drain(store, limit=1) == 1
    assert queue.next()['capture_id'] == following
    assert store.conn.execute(f'SELECT count(*) FROM {relation}').fetchone()[0] == 1
    responses = store.rec_ledger.evidence().records('response')
    assert {row['opportunity_id'] for row in responses} == {offer}
    if kind == 'player':
        event_id = store.player_events.event_for_capture(main, 275, capture)
        assert [(row['kind'], row['id']) for row in responses] == [('experience', f'player:{event_id}')]
    else:
        assert sorted(row['kind'] for row in responses) == ['experience', 'start']
        binding = store.rec_ledger.choice_playback(main, 'PL', 'known', 276, 3600)
        assert binding['opportunity_id'] == offer and binding['at'] == 275
    assert playback_feedback.drain(store) == 1
    assert playback_feedback.drain(store) == 0
    assert store.rec_ledger.evidence().records('response') == responses


@pytest.mark.parametrize('kind, body, relation', [
    ('player', {'kind': 'next', 'videoId': 'known', 'position': 20, 'duration': 180}, 'player_events'),
    ('live', {'videoId': 'known', 'title': 'Song', 'artist': 'Artist'}, 'play_events'),
    ('rating', {'video_id': 'known', 'status': 'LIKE'}, 'confirmed_rating_actions'),
])
def test_interrupted_projection_rolls_back_before_retry(library, monkeypatch, kind, body, relation):
    store, main, _, _, _ = library
    capture = playback_feedback.capture(store, kind, main, body, 100)
    apply = playback_feedback._apply

    def interrupted(owner, row):
        apply(owner, row)
        raise RuntimeError('interrupted before receipt')

    monkeypatch.setattr(playback_feedback, '_apply', interrupted)
    with pytest.raises(RuntimeError, match='before receipt'):
        playback_feedback.drain(store)
    assert store.conn.execute(f'SELECT count(*) FROM {relation}').fetchone()[0] == 0
    assert store.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0] == 0
    assert playback_feedback.inbox(store).pending() == 1
    monkeypatch.setattr(playback_feedback, '_apply', apply)
    assert playback_feedback.drain(store) == 1
    assert store.conn.execute(f'SELECT count(*) FROM {relation}').fetchone()[0] == 1
    assert store.conn.execute('SELECT capture_id FROM playback_feedback_receipts').fetchone()[0] == capture


def test_paused_live_play_exit_and_observed_rating_keep_original_account_and_clock(library, monkeypatch):
    store, main, other, _, _ = library
    ctx = SimpleNamespace(store=store)
    monkeypatch.setattr('yt_playlist.core.features.listening_charts_enabled', lambda: True)
    msg = {'videoId': 'known', 'title': 'Song', 'artist': 'Artist', 'playlist': 'PL',
           'brandKnown': True, 'brandId': 'other', 'likeStatus': 'LIKE', 'timeZone': 'UTC'}
    with paused_recording(store):
        assert live_plays.handle_play_event(ctx, msg, 100)
        assert player_events.handle_player_event(ctx, {**msg, 'kind': 'track_exit',
            'position': 40, 'duration': 180}, 140)
        assert player_events.handle_player_event(ctx, {'kind': 'rate', 'brandKnown': True, 'brandId': 'other',
            'url': 'https://music.youtube.com/youtubei/v1/like/dislike',
            'body': json.dumps({'target': {'videoId': 'unseen'}}),
            'phase': 'settled', 'success': True, 'httpStatus': 200}, 141)
        assert not player_events.handle_player_event(ctx, {**msg, 'kind': 'next', 'brandKnown': False}, 142)
        queue = playback_feedback.inbox(store)
        assert queue.pending() == 3
        rows = json.loads(fao.read(queue.path).bytes)['captures']
        assert [(row['kind'], row['account_id'], row['observed_at']) for row in rows] == [
            ('live', other, 100), ('player', other, 140), ('player', other, 141)]
        assert playback_feedback.drain(store) == 0
        assert not store.conn.execute('SELECT 1 FROM play_events').fetchone()
        assert not store.conn.execute('SELECT 1 FROM player_events').fetchone()
        assert not store.conn.execute('SELECT 1 FROM playback_feedback_receipts').fetchone()
    assert playback_feedback.drain(store, limit=1) == 1
    assert playback_feedback.inbox(store).pending() == 2
    # Ordinary sync cannot leapfrog the retained feedback prefix.
    ratings.record_observations(store, other, {}, source='sync', now=139)
    assert not playback_feedback.inbox(store).pending()
    play = store.conn.execute('SELECT account_id,video_id,played_at FROM play_events').fetchone()
    assert tuple(play) == (other, 'known', 100)
    rows = list(map(tuple, store.conn.execute('SELECT account_id,kind,at FROM player_events ORDER BY at,id')))
    assert rows == [(other, 'listen_start', 100), (other, 'track_exit', 140),
                    (other, 'rate', 141), (other, 'confirmed_rating', 141)]
    assert store.tracks.item_rating(other, 'unseen') == {'source': 'api', 'status': 'DISLIKE', 'observed_at': 141}
    assert store.tracks.item_rating(main, 'unseen') is None


def test_failed_remote_rating_is_visible_and_never_acknowledged_or_replayed(library):
    store, main, _, _, _ = library
    store._evidence_recovery_pending = True
    remote = SimpleNamespace(rate_song=lambda *_: {'status': 'STATUS_FAILED'})
    with pytest.raises(ValueError, match='wouldn’t update'):
        ratings.set_rating(store, main, 'known', 'LIKE', remote, 100)
    assert not playback_feedback.inbox(store).pending()
    assert store.tracks.item_rating(main, 'known') is None


def test_discovery_heart_and_later_unlike_survive_maintenance_and_restart(library):
    store, main, other, _, lm = library
    key = 'discovery song|artist'
    store.discovery.upsert_discovered_track(key, 'discovered', 'Discovery song', 'Artist',
        'Album', None, None, None, 'run', 100)
    remote, wrong_account = FakeClient(), FakeClient()
    app = create_app(store, lambda: {main: remote, other: wrong_account}, now_fn=lambda: 1000,
                     background_work=False)
    store._evidence_recovery_pending = True
    entered, release = threading.Event(), threading.Event()

    def hold_writer():
        with store._lock, store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            entered.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(hold_writer)
        try:
            assert entered.wait(5)
            client = TestClient(app, base_url='http://127.0.0.1')
            started = time.monotonic()
            response = client.post('/discovery/add', data={'kind': 'track', 'id': key})
            assert response.status_code == 200 and not response.text
            app.state.ctx.ops().set_rating('discovered', 'INDIFFERENT')
            assert time.monotonic() - started < 2
            assert playback_feedback.inbox(store).pending() == 2
        finally:
            release.set()
        writer.result(timeout=5)
    assert remote.rated == [('discovered', 'LIKE'), ('discovered', 'INDIFFERENT')]
    assert not wrong_account.rated
    path = store._db_path
    store.conn.close()
    resumed = Store(path)
    try:
        resumed.conn.execute('PRAGMA synchronous=OFF')
        resumed.init_schema()
        assert playback_feedback.drain(resumed) == 2
        assert resumed.tracks.item_rating(main, 'discovered')['status'] == 'INDIFFERENT'
        assert resumed.conn.execute("SELECT 1 FROM track_sources WHERE video_id='discovered' "
                                   'AND library_origin IS NOT NULL').fetchone()
        assert not resumed.playlists.get_playlist_track_ids(lm)
        assert resumed.conn.execute('SELECT count(*) FROM confirmed_rating_actions').fetchone()[0] == 2
        assert not resumed.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        resumed.conn.close()


def test_local_capture_failure_does_not_report_a_confirmed_remote_action_as_rejected(library, monkeypatch):
    store, main, _, _, _ = library
    remote = FakeClient()
    app = create_app(store, lambda: {main: remote}, now_fn=lambda: 1000, background_work=False)
    ctx, commands = app.state.ctx, []
    ctx.bridge._send = lambda _: None
    ctx.bridge.account_bound_next = True
    ctx.bridge.now_playing = playing()
    ctx.bridge.now_playing_seen_at = time.monotonic()
    store._evidence_recovery_pending = True

    def failed_capture(*args, **kwargs):
        raise OSError('controlled local storage failure')

    async def control(frame, timeout=10):
        commands.append(frame)
        return 200, '{"ok":true}'

    monkeypatch.setattr(playback_feedback.inbox(store), 'capture', failed_capture)
    monkeypatch.setattr(ctx.bridge, 'request_control', control)
    client = TestClient(app, base_url='http://127.0.0.1')
    result = client.post('/now-playing/rate', json={
        'action': 'like', 'videoId': 'known', 'likeStatus': 'LIKE'}).json()
    assert result['ok'] and not result['error']
    assert result['player_error'] == 'Rating saved. Some updates are not confirmed; check it in YouTube Music.'
    result = client.post('/now-playing/next', json={'videoId': 'known'}).json()
    assert result['ok'] and 'Playback advanced' in result['player_error']
    assert remote.rated == [('known', 'LIKE')]
    assert [frame['type'] for frame in commands] == ['rate', 'next']
