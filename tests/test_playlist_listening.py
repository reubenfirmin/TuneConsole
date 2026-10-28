"""Playlist activity requires its own account-matched playback context."""
import json
import re
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.library import live_plays, player_events
from yt_playlist.web.app import create_app


def seed(store, created=1_000_000):
    account = store.identities.upsert_identity('Main', 'cred', None, True)
    track = store.tracks.upsert_track('song', 'Sombrero', 'Phase 09', None, 300)
    playlist = store.playlists.upsert_playlist(account, 'PLphase09', 'Phase 09 - Sombrero (2021 Remaster)',
                                              1, 'hash', created, created_at=created)
    store.playlists.set_playlist_tracks(playlist, [track])
    return account, playlist, store.tracks.identity_key_for_video('song')


def observe(store, at, context='PLphase09', *, kind='state', state='playing', position=12, brand=''):
    return player_events.handle_player_event(SimpleNamespace(store=store), {
        'kind': kind, 'videoId': 'song', 'playlist': context, 'position': position,
        'duration': 300, 'state': state, 'brandId': brand}, at)


def play(store, at, context='PLphase09', **fields):
    return live_plays.handle_play_event(SimpleNamespace(store=store), {
        'title': 'Sombrero', 'artist': 'Phase 09', 'videoId': 'song',
        'playlist': context, 'brandId': '', **fields}, at)


def test_member_history_and_playing_elsewhere_do_not_become_playlist_listens(store):
    account, playlist, key = seed(store)
    # The reported case: a new playlist contains a song heard four months ago.
    store.history.record_history_plays(account, 1_000_000 - 120 * 86400, [key], source_timezone='UTC')
    assert store.charts.get_playlist_listen_stats()[playlist][1] == 1
    assert store.charts.get_playlist_playback_stats() == {}
    for index, context in enumerate(('PLdifferent', 'OLAKalbum', 'RDdifferent', None)):
        assert play(store, 1_000_000 + index * 2000, context)
        observe(store, 1_000_001 + index * 2000, context, kind='tick')
    assert store.charts.get_playlist_playback_stats() == {}
    assert store.charts.get_playlist_listen_stats()[playlist][1] > 1


def test_partial_listening_counts_once_across_duplicates_ticks_and_track_changes(store):
    _, playlist, _ = seed(store)
    assert play(store, 1_000_000)
    assert not play(store, 1_000_001, likeStatus='LIKE')
    for _ in range(2):
        observe(store, 1_000_001)
        observe(store, 1_000_015, kind='tick')
    observe(store, 1_000_020, state='paused')
    assert store.charts.get_playlist_playback_stats()[playlist] == (1_000_015, 1)
    # The next track belongs to the same listening session; neither track needs to finish.
    player_events.handle_player_event(SimpleNamespace(store=store), {
        'kind': 'state', 'state': 'playing', 'videoId': 'next-song', 'playlist': 'PLphase09'}, 1_000_050)
    assert store.charts.get_playlist_playback_stats()[playlist] == (1_000_050, 1)
    assert len(store.history.play_events_since(0)) == 1


def test_context_changes_and_thirty_minute_gaps_start_new_sessions(store):
    _, playlist, _ = seed(store)
    observe(store, 1_000_000)
    observe(store, 1_000_010, 'OLAKalbum')
    observe(store, 1_000_020)
    observe(store, 1_001_820, kind='tick')  # exactly thirty minutes is still the same session
    assert store.charts.get_playlist_playback_stats()[playlist] == (1_001_820, 2)
    observe(store, 1_003_621, kind='tick')
    assert store.charts.get_playlist_playback_stats()[playlist] == (1_003_621, 3)


def test_native_context_observations_survive_same_song_play_report_deduplication(store):
    _, playlist, _ = seed(store)
    assert play(store, 1_000_000, 'OLAKalbum')
    assert not play(store, 1_000_005)
    # The track-history deduper keeps one song play, while native player events
    # independently establish the new playlist context and its partial listen.
    observe(store, 1_000_006, kind='tick')
    assert len(store.history.play_events_since(0)) == 1
    assert store.charts.get_playlist_playback_stats()[playlist] == (1_000_006, 1)


def test_account_ownership_normalized_ids_and_known_creation_boundaries(store):
    _, playlist, _ = seed(store)
    other = store.identities.upsert_identity('Other', 'cred', 'other-brand', False)
    other_playlist = store.playlists.upsert_playlist(other, 'PLphase09', 'Other account', 1, '', 0,
                                                    created_at=1_000_000)
    assert play(store, 999_999)
    observe(store, 999_999, kind='tick')
    assert store.charts.get_playlist_playback_stats() == {}
    # A real report from the other account only belongs to that account's playlist.
    observe(store, 1_000_000, brand='other-brand')
    assert store.charts.get_playlist_playback_stats() == {other_playlist: (1_000_000, 1)}
    for index, context in enumerate(('RDAMPLPLphase09', 'VLPLphase09', 'PLphase09')):
        observe(store, 1_000_000 + index, context)
    assert store.charts.get_playlist_playback_stats() == {
        playlist: (1_000_002, 1), other_playlist: (1_000_000, 1)}


def test_paused_cued_muted_and_unverified_reports_do_not_invent_a_listen(store):
    seed(store)
    observe(store, 1_000_001, state='paused')
    observe(store, 1_000_002, kind='tick', position=0)
    for index, fields in enumerate(({'paused': True}, {'muted': True}, {'volume': 0},
                                    {'brandKnown': False}, {'metadataVerified': False})):
        assert not play(store, 1_000_010 + index, **fields)
    assert store.charts.get_playlist_playback_stats() == {}


@pytest.mark.parametrize('kind', ['tick', 'state'])
@pytest.mark.parametrize('fields', [{'muted': True}, {'tabMuted': True}, {'volume': 0}, {'paused': True}])
def test_silent_native_observations_do_not_invent_a_listen(store, kind, fields):
    seed(store)
    assert player_events.handle_player_event(SimpleNamespace(store=store), {
        'kind': kind, 'state': 'playing', 'videoId': 'song', 'playlist': 'PLphase09',
        'position': 12, **fields}, 1_000_001)
    assert store.charts.get_playlist_playback_stats() == {}


@pytest.mark.parametrize('mute', ['tab', 'media', 'zero', 'nonowner'])
@pytest.mark.parametrize('sole', [False, True], ids=['two-tabs', 'sole-tab'])
def test_real_relay_only_records_audible_owner_playlist_listening(store, mute, sole):
    _, playlist, _ = seed(store)
    script = r'''
const fs = require('node:fs'), vm = require('node:vm');
const [mute, sole] = process.argv.slice(1), frames = [];
const tabs = [{id: 2, audible: false, active: true, mutedInfo: {muted: mute === 'tab'}}];
if (sole === 'False') tabs.unshift({id: 1, audible: true, active: false});
const context = vm.createContext({URL, setTimeout, clearTimeout,
  createNativeRatings: () => ({observe() {}, removed() {}})});
vm.runInContext(fs.readFileSync('extension/worker/playback.js', 'utf8'), context);
context.options = {origin: 'https://music.youtube.com', appOrigin: 'http://127.0.0.1:8765',
  musicTabs: {list: async () => tabs}, sensors: {},
  send: frame => { frames.push(frame); return true; }, warn: error => { throw Error(error); }};
const playback = vm.runInContext('createPlayback(options)', context);
async function report(id, frame) {
  playback.receive(frame, {tab: tabs.find(tab => tab.id === id)});
  await playback.drained;
}
(async () => {
  const frame = {videoId: 'song', playlist: 'PLphase09', brandKnown: true, brandId: '',
    position: 12, state: 'playing'};
  if (sole === 'False') await report(1, {type: 'now-heartbeat', paused: false, muted: false});
  // Fresh state events arrive before the matching now-playing heartbeat.
  // Tab-level mute comes from Chrome; media mute/zero volume comes from MAIN.
  await report(2, {...frame, type: 'pevent', kind: 'state',
    muted: mute === 'media', volume: mute === 'zero' ? 0 : 1});
  await report(2, {...frame, type: 'now-heartbeat', paused: false,
    muted: mute === 'media', volume: mute === 'zero' ? 0 : 1});
  // Legacy ticks may omit mute state: the relay must still apply ownership.
  await report(2, {...frame, type: 'pevent', kind: 'tick'});
  const before = frames.filter(frame => frame.type === 'pevent').length;
  // Hand playback to the second tab and change into the target playlist.
  if (sole === 'False') { tabs[0].audible = false; await report(1, {type: 'now-heartbeat', paused: true}); }
  const target = tabs.find(tab => tab.id === 2);
  target.mutedInfo.muted = false; target.audible = true;
  await report(2, {...frame, type: 'now-heartbeat', playlist: 'PLdifferent', paused: false, muted: false, volume: 1});
  await report(2, {...frame, playlist: 'PLdifferent', type: 'pevent', kind: 'tick'});
  await report(2, {...frame, type: 'pevent', kind: 'tick', muted: false, volume: 1});
  console.log(JSON.stringify({before, frames}));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run(['node', '-e', script, mute, str(sole)], text=True,
                            capture_output=True, check=True, timeout=20,
                            cwd=Path(__file__).resolve().parents[1])
    output = json.loads(result.stdout)
    expected = 2 if sole and mute == 'nonowner' else 0
    assert output['before'] == expected
    observations = [frame for frame in output['frames'] if frame['type'] == 'pevent']
    for index, frame in enumerate(observations):
        player_events.handle_player_event(SimpleNamespace(store=store), frame, 1_000_001 + index)
    # Only genuinely audible owner events count, including partial listening
    # after changing context. A later qualifying context starts a new session.
    assert store.charts.get_playlist_playback_stats() == {
        playlist: (1_000_001 + len(observations) - 1, 2 if expected else 1)}


def test_playlist_page_exposes_creation_separately_from_actual_partial_listening(store):
    account, playlist, key = seed(store)
    store.history.record_history_plays(account, 1_000_000 - 120 * 86400, [key], source_timezone='UTC')
    with TestClient(create_app(store, lambda: {account: FakeClient()}, now_fn=lambda: 1_086_400),
                    base_url='http://127.0.0.1') as web:
        def row():
            markup = web.get('/playlists').text
            rows = json.loads(re.search(r'playlistsTab\((\[.*?\])\)', markup)[1])
            return next(row for row in rows if row['id'] == playlist)

        initial = row()
        assert initial['is_new'] and initial['created'] == 1_000_000
        assert initial['last'] is None and initial['listens'] == 0
        observe(store, 1_086_390, kind='tick')
        partial = row()
        assert partial['created'] == 1_000_000
        assert partial['last'] == 1_086_390 and partial['listens'] == 1
