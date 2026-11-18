"""First sounds follows the playing playlist and reconciles additions after restart."""
import json
import time
from types import SimpleNamespace

import pytest

from tests.conftest import FakeClient, _track
from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.core.store import Store
from yt_playlist.controllers.playback_observations import player_snapshot
from yt_playlist.library.generated_composition import commands
from yt_playlist.web.first_sounds import FirstSounds, KEY, _saved_position
from yt_playlist.web.radio_playback import PlaybackObservation, RadioPlayback


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'),)) as schema:
        yield schema


def setup_stream(store, monkeypatch):
    identity = store.accounts.upsert_account('Main', 'bridge', None, True)
    client = FakeClient(tracks={'source': [_track(f'v{i}', f'Song {i}', 'Artist') for i in range(6)]})
    tracks = []
    for i in range(3):
        entry = store.tracks.upsert_track(f'v{i}', f'Song {i}', 'Artist', duration_s=200, available=True)
        tracks.append(dict(video_id=f'v{i}', key=store.tracks.catalog_subject(entry)['identity_key'],
                           title=f'Song {i}', artist='Artist', duration=200))
    result = commands(store, 1).create('First sounds', tracks, client=client, account_id=identity)
    ctx = SimpleNamespace(store=store, client_provider=lambda: {identity: client}, now_fn=lambda: 10,
                          bridge=SimpleNamespace(), work=SimpleNamespace(accepting=True))
    stream = FirstSounds(ctx)
    stream.begin(result)
    playing = {'playlist_ytm_id': result['new_ytm'], 'video_id': 'v0', 'paused': False}
    observations = []

    def wire(owner):
        monkeypatch.setattr(owner.playback, 'current', lambda: playing if not playing.get('paused') else {})
        def observe(request):
            observations.append(request)
            return PlaybackObservation({'player': dict(playing), 'playback_synced': True}, time.monotonic())
        monkeypatch.setattr(owner.playback, 'observe', observe)
    wire(stream)
    return stream, ctx, client, playing, observations, wire


def test_first_sounds_adds_new_imports_and_prepared_recommendations_without_switching_playlist(
        file_backed_store, monkeypatch):
    from tests.onboarding_support import publish_personal_mix
    store = file_backed_store
    stream, ctx, client, playing, observations, _ = setup_stream(store, monkeypatch)
    # No extra music yet: leave the native queue and current song untouched.
    stream.maintain()
    assert len(stream.state['tracks']) == 3
    store.tracks.upsert_track('v3', 'Song 3', 'Artist', duration_s=200, available=True)
    stream.maintain()
    playlist = stream.state['playlist']
    assert [row['videoId'] for row in client.get_playlist(playlist)['tracks']] == ['v0', 'v1', 'v2', 'v3']
    assert observations[-1].refresh
    assert playing['video_id'] == 'v0'
    suggestions = publish_personal_mix(store, now=10, count=1)
    client._catalog.update((track['video_id'], _track(track['video_id'], track['title'], track['artist']))
                           for track in suggestions)
    playing['video_id'] = 'v1'
    stream.maintain()
    assert len(stream.state['tracks']) == 5
    assert stream.state['tracks'][-1]['video_id'] not in {'v0', 'v1', 'v2', 'v3'}
    assert all(request.playlist == playlist for request in observations)
    assert store.settings.get_setting('radio_current_session') is None
    # A later different listening choice must not receive continuation writes.
    playing['playlist_ytm_id'] = 'OTHER'
    stream.maintain()
    assert len(client.get_playlist(playlist)['tracks']) == 5


def test_lost_append_reply_survives_file_reopen_without_replaying_the_addition(
        file_backed_store, monkeypatch):
    store = file_backed_store
    stream, ctx, client, playing, observations, wire = setup_stream(store, monkeypatch)
    store.tracks.upsert_track('v3', 'Song 3', 'Artist', duration_s=200, available=True)
    write, read = client.add_playlist_items, client.get_playlist
    additions = []
    offline = False

    def lost_reply(playlist, videos, **kwargs):
        nonlocal offline
        additions.extend(videos)
        write(playlist, videos, **kwargs)
        offline = True
        raise ConnectionError('Reply was lost after saving')

    def readback(playlist, limit=None):
        if offline:
            raise ConnectionError('Readback is temporarily unavailable')
        return read(playlist, limit)

    monkeypatch.setattr(client, 'add_playlist_items', lost_reply)
    monkeypatch.setattr(client, 'get_playlist', readback)
    with pytest.raises(ConnectionError):
        stream.maintain()
    saved = json.loads(store.settings.get_setting(KEY))
    assert saved['pending_sent'] and saved['pending']['video_id'] == 'v3'
    reopened = Store(store._db_path)
    reopened._bind_repositories()
    try:
        ctx.store = reopened
        resumed = FirstSounds(ctx)
        wire(resumed)
        offline = False
        resumed.maintain()
        assert additions == ['v3']
        assert [track['video_id'] for track in resumed.state['tracks']] == ['v0', 'v1', 'v2', 'v3']
        assert resumed.state['pending'] is None
        assert observations[-1].refresh
    finally:
        reopened.conn.close()


def test_external_playlist_edit_settles_across_heartbeats_without_overwriting_it(
        file_backed_store, monkeypatch):
    from yt_playlist.web.radio_mutations import CONFIRM_TIMEOUT

    store = file_backed_store
    stream, ctx, client, playing, observations, _ = setup_stream(store, monkeypatch)
    store.tracks.upsert_track('v3', 'Song 3', 'Artist', duration_s=200, available=True)
    playlist = stream.state['playlist']
    client._tracks[playlist].append(_track('v5', 'Added elsewhere', 'Artist'))
    before = list(client.get_playlist(playlist)['tracks'])
    writes = list(client.added)
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    ctx.work.start_thread = lambda action, **options: action()

    stream.prepare()
    assert not stream.state['stopped']
    clock[0] += CONFIRM_TIMEOUT + 1
    stream.prepare()
    assert stream.state['stopped']
    assert json.loads(store.settings.get_setting(KEY))['stopped']
    assert client.get_playlist(playlist)['tracks'] == before
    assert client.added == writes


def test_saved_addition_keeps_refreshing_until_the_native_queue_confirms_it(
        file_backed_store, monkeypatch):
    store = file_backed_store
    stream, ctx, client, playing, observations, _ = setup_stream(store, monkeypatch)
    store.tracks.upsert_track('v3', 'Song 3', 'Artist', duration_s=200, available=True)
    loaded = [False]

    def observe(request):
        observations.append(request)
        return PlaybackObservation({'player': dict(playing), 'playback_synced': loaded[0]}, time.monotonic())

    monkeypatch.setattr(stream.playback, 'observe', observe)
    stream.maintain()
    assert stream.state['refresh']
    writes = list(client.added)
    stream.maintain()
    assert observations[-1].refresh
    assert stream.state['refresh']
    loaded[0] = True
    stream.maintain()
    assert not stream.state['refresh']
    stream.maintain()
    assert not observations[-1].refresh
    assert client.added == writes
    assert playing['video_id'] == 'v0'


@pytest.mark.parametrize('case', ['direct', 'counterpart', 'unrelated', 'wrong-playlist', 'wrong-slot', 'stale-slot'])
def test_native_counterpart_continues_only_its_confirmed_saved_slot(file_backed_store, monkeypatch, case):
    store = file_backed_store
    stream, ctx, client, _, _, _ = setup_stream(store, monkeypatch)
    playlist = stream.state['playlist']
    actual = 'v0' if case == 'direct' else 'native-v0'
    frame = {'videoId': actual, 'playlist': playlist, 'paused': False, 'position': 12}
    if case not in {'direct', 'unrelated'}:
        frame['queueItem'] = {'videoId': 'v0', 'playlist': 'OTHER' if case == 'wrong-playlist' else playlist,
                              'index': 1 if case == 'wrong-slot' else 0}
    snapshot = player_snapshot(frame)
    requests = []

    async def request_control(payload):
        requests.append(payload)
        return 200, json.dumps({'ok': True, 'player': {
            'video_id': 'changed' if case == 'stale-slot' else actual,
            'playlist_ytm_id': playlist, 'paused': False, 'position': 12}, 'playback_synced': False})

    ctx.bridge = SimpleNamespace(connected=True, now_playing=snapshot,
        now_playing_seen_at=time.monotonic(), request_control=request_control)
    stream.playback = RadioPlayback(ctx.bridge)
    store.tracks.upsert_track('v3', 'Song 3', 'Artist', duration_s=200, available=True)
    stream.maintain()
    expected = ['v0', 'v1', 'v2', 'v3'] if case in {'direct', 'counterpart'} else ['v0', 'v1', 'v2']
    assert [row['videoId'] for row in client.get_playlist(playlist)['tracks']] == expected
    assert [track['video_id'] for track in stream.state['tracks']] == expected
    assert requests[-1]['videoIds'] == expected
    assert ctx.bridge.now_playing == snapshot  # Actual upload and position stay intact.
    assert store.settings.get_setting('radio_current_session') is None


@pytest.mark.parametrize(('item', 'expected'), [
    ({'video_id': 'planned', 'index': 2}, 2),
    ({'video_id': 'planned'}, -1),  # Repeated uploads need a proven occurrence.
    ({'video_id': 'planned', 'index': 1}, -1),
    ({'video_id': 'planned', 'index': True}, -1),
    ({'video_id': 'planned', 'index': 3}, -1),
    ({'video_id': 'native'}, 1),  # Older endpoints can identify a unique saved upload.
])
def test_saved_slot_precedes_an_actual_upload_elsewhere_in_the_playlist(item, expected):
    tracks = [{'video_id': video} for video in ['planned', 'native', 'planned']]
    player = {'video_id': 'native', 'playlist_ytm_id': 'PL'}
    playing = {**player, 'queue_item': {**item, 'playlist': 'PL'}}
    assert _saved_position(tracks, player, playing) == expected
