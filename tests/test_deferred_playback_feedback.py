"""Confirmed playback controls retain local evidence while maintenance owns SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.playback_feedback_support import held_copy
from yt_playlist.core import playback_feedback
from yt_playlist.core.store import Store
from yt_playlist.library import live_plays, player_events, ratings
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.bridge import _now_playing_snapshot


@pytest.fixture
def library(file_backed_store):
    store = file_backed_store
    main = store.accounts.upsert_account('Main', 'bridge', None, True)
    other = store.accounts.upsert_account('Other', 'bridge', 'other', False)
    entry = store.tracks.upsert_track('known', 'Song', 'Artist', None, 180)
    lm = store.playlists.upsert_playlist(main, 'LM', 'Liked Music', 0, '', 1)
    return store, main, other, entry, lm


def playing(video='known'):
    return _now_playing_snapshot({'videoId': video, 'title': 'Song', 'artist': 'Artist',
        'playlist': 'PLplaying', 'position': 15, 'duration': 180, 'brandKnown': True, 'brandId': ''})


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


def test_feedback_requires_its_owned_file_before_replay_or_acknowledgement(library):
    store, main, _, _, _ = library
    queue = playback_feedback.inbox(store)
    playback_feedback.capture(store, 'player', main, {
        'kind': 'next', 'videoId': 'known', 'position': 20, 'duration': 180}, 100)
    row = queue.next()
    path = Path(row['filename'])
    original = path.read_bytes()
    with closing(sqlite3.connect(queue.path)) as conn:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(playback_feedback_inbox)')}
    assert 'body' not in columns and 'account_id' in columns
    path.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='retained content'):
        playback_feedback.drain(store)
    assert queue.pending() == 1
    assert not store.conn.execute('SELECT 1 FROM playback_feedback_receipts').fetchone()
    assert not store.conn.execute('SELECT 1 FROM player_events').fetchone()
    path.write_bytes(original)
    assert playback_feedback.drain(store) == 1
    assert not queue.pending() and not path.exists()


def test_earlier_inbox_relocation_keeps_capture_identity_order_and_account(library):
    store, main, _, _, _ = library
    queue = playback_feedback.inbox(store)
    body = {'kind': 'next', 'videoId': 'known', 'position': 20, 'duration': 180}
    with closing(sqlite3.connect(queue.path)) as conn, conn:
        conn.execute('CREATE TABLE playback_feedback_inbox ('
            'sequence INTEGER PRIMARY KEY AUTOINCREMENT,capture_id TEXT NOT NULL UNIQUE,'
            'kind TEXT NOT NULL,account_id INTEGER NOT NULL,observed_at REAL NOT NULL,'
            'video_id TEXT,rating_status TEXT,body TEXT NOT NULL)')
        conn.execute('INSERT INTO playback_feedback_inbox VALUES (7,?,?,?,?,?,?,?)',
                     ('original-capture', 'player', main, 100, 'known', None, json.dumps(body)))
    row = queue.next()
    assert row['sequence'] == 7 and row['capture_id'] == 'original-capture'
    assert row['account_id'] == main and row['observed_at'] == 100
    assert queue.body(row) == body
    assert playback_feedback.drain(store) == 1
    assert store.conn.execute('SELECT capture_id FROM playback_feedback_receipts').fetchone()[0] == 'original-capture'
    assert store.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 1


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
    with held_copy(store):
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
        assert 'likes and Next are available' in status['message']
        assert client.post('/sync').status_code == 503
        assert ratings.main_rating(store, 'known')['status'] == 'INDIFFERENT'
        assert playback_feedback.drain(store) == 0
        assert not store.conn.execute('SELECT 1 FROM playback_feedback_receipts').fetchone()
    assert radio_calls == []
    assert remote.rated == [('known', status) for status in ('LIKE', 'INDIFFERENT', 'DISLIKE', 'INDIFFERENT')] + [
        ('unseen', 'DISLIKE')]
    assert wrong_account.rated == []
    assert [frame['videoId'] for frame in commands if frame['type'] == 'next'] == ['known', 'known']
    assert store.conn.execute('SELECT count(*) FROM item_ratings').fetchone()[0] == 0
    path = store._db_path
    store.conn.close()
    resumed = Store(path)
    try:
        resumed.init_schema()
        assert playback_feedback.drain(resumed) == 7
        assert not playback_feedback.inbox(resumed).pending()
        assert resumed.tracks.item_rating(main, 'known')['status'] == 'INDIFFERENT'
        assert resumed.tracks.item_rating(main, 'unseen')['status'] == 'DISLIKE'
        assert resumed.playlists.get_playlist_track_ids(lm) == []
        rows = list(resumed.conn.execute("SELECT account_id,video_id,position,duration,at FROM player_events WHERE kind='next'"))
        assert list(map(tuple, rows)) == [(main, 'known', 15, 180, 1000)] * 2
        assert resumed.conn.execute('SELECT count(*) FROM listening_rating_actions').fetchone()[0] == 5
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
        resumed.init_schema()
        assert playback_feedback.drain(resumed) == 1
        assert playback_feedback.drain(resumed) == 0
        assert resumed.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 1
        assert resumed.player_events.skip_blackouts(101) == {'song|artist': 100 + 86400}
    finally:
        resumed.conn.close()


def test_partial_repository_writes_roll_back_with_the_receipt_and_retry(library, monkeypatch):
    store, main, _, _, _ = library
    capture = playback_feedback.capture(store, 'player', main, {
        'kind': 'next', 'videoId': 'known', 'position': 20, 'duration': 180}, 100)
    apply = playback_feedback._apply

    def interrupted(owner, row):
        apply(owner, row)  # PlayerEventsRepo calls commit explicitly.
        raise RuntimeError('interrupted before receipt')

    monkeypatch.setattr(playback_feedback, '_apply', interrupted)
    with pytest.raises(RuntimeError, match='before receipt'):
        playback_feedback.drain(store)
    assert store.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 0
    assert store.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0] == 0
    assert playback_feedback.inbox(store).pending() == 1
    monkeypatch.setattr(playback_feedback, '_apply', apply)
    assert playback_feedback.drain(store) == 1
    assert store.conn.execute('SELECT capture_id FROM playback_feedback_receipts').fetchone()[0] == capture


def test_paused_live_play_exit_and_observed_rating_keep_original_account_and_clock(library, monkeypatch):
    store, main, other, _, _ = library
    ctx = SimpleNamespace(store=store)
    monkeypatch.setattr('yt_playlist.core.features.listening_charts_enabled', lambda: True)
    msg = {'videoId': 'known', 'title': 'Song', 'artist': 'Artist', 'playlist': 'PL',
           'brandKnown': True, 'brandId': 'other', 'likeStatus': 'LIKE', 'timeZone': 'UTC'}
    with held_copy(store):
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
        with closing(queue.connect()) as conn:
            assert list(map(tuple, conn.execute('SELECT kind,account_id,observed_at '
                'FROM playback_feedback_inbox ORDER BY sequence'))) == [
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
    assert rows == [(other, 'listen_start', 100), (other, 'track_exit', 140), (other, 'rate', 141)]
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
        resumed.init_schema()
        assert playback_feedback.drain(resumed) == 2
        assert resumed.tracks.item_rating(main, 'discovered')['status'] == 'INDIFFERENT'
        assert resumed.conn.execute("SELECT 1 FROM tracks WHERE video_id='discovered'").fetchone()
        assert not resumed.playlists.get_playlist_track_ids(lm)
        assert resumed.conn.execute('SELECT count(*) FROM listening_rating_actions').fetchone()[0] == 2
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
