"""Exercise the real MAIN-world radio observer against independent playback and page state."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", [
    "native_then_page", "strict_future", "retries_and_drift", "endpoint_guard",
    "other_playlist", "missing_sidebar", "refresh_errors", "reinjection",
    "counterpart_refresh", "counterpart_confirmed", "counterpart_trimmed", "counterpart_repeat",
    "unrelated_selected", "counterpart_other_playlist", "counterpart_wrong_index", "counterpart_stale_endpoint",
    "removed_current", "missing_row", "missing_row_repeat", "missing_row_wrong_playlist",
    "malformed_automix", "malformed_counterpart", "playability_identity",
])
def test_radio_native_refresh_and_playlist_view(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the radio observer")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[1];
const code = fs.readFileSync('extension/radio_session.js', 'utf8');
let now = 10000, current = 'v2', playlist = 'PLSESSION', nativePlaylist = playlist;
let nativeIds = ['v1', 'v2', 'old-next'], available = true;
let pageIds = ['v1', 'v2', 'old-next'], sidebarAvailable = true;
let nativeFails = false, viewFails = false, clicks = 0, toggleClicks = 0;
let expected = ['v1', 'v2', 'v3', 'v4'];
let selected = null, counterpart = null, savedIndex;
if (scenario.startsWith('counterpart_') || scenario === 'unrelated_selected') {
  current = 'native-v2';
  counterpart = scenario === 'unrelated_selected' ? 'unrelated' : current;
  selected = 1;
  savedIndex = scenario === 'counterpart_wrong_index' ? 0 : 1;
  if (scenario === 'counterpart_confirmed') nativeIds = expected;
  if (scenario === 'counterpart_trimmed') { nativeIds = ['v2', 'v3', 'v4']; selected = 0; }
  if (scenario === 'counterpart_repeat') {
    expected = ['v1', 'v2', 'v3', 'v2', 'v4'];
    nativeIds = expected;
    selected = savedIndex = 3;
  }
  if (scenario === 'counterpart_other_playlist') nativePlaylist = 'OTHER';
}
if (scenario === 'removed_current') {
  current = 'retired';
  nativeIds = [current];
}
if (scenario.startsWith('missing_row')) {
  nativeIds = scenario === 'missing_row_wrong_playlist' ? ['old-next'] : [];
  if (scenario === 'missing_row_wrong_playlist') nativePlaylist = 'OTHER';
  if (scenario === 'missing_row_repeat') expected = ['v1', 'v2', 'v3', 'v2', 'v4'];
}
if (scenario === 'malformed_counterpart') selected = 1;
const initialVideo = current;
const commands = [], listeners = [];
const video = {paused: false, currentTime: 30, duration: 300};
const endpoint = {clickTrackingParams: 'tracking', watchEndpoint: {
  videoId: current, playlistId: playlist, index: 7, startTimeSeconds: 123,
  playlistSetVideoId: 'set-v2', playerParams: 'music-config',
  watchEndpointMusicSupportedConfigs: {watchEndpointMusicConfig: {musicVideoType: 'ATV'}},
}};
if (scenario === 'counterpart_stale_endpoint') endpoint.watchEndpoint.videoId = 'v2';
const forbidden = () => { throw Error('unexpected playback mutation'); };
const api = {
  getVideoData: () => ({video_id: current}), getPlaylistId: () => playlist,
  // Deliberately disagree with the native application queue.
  getPlaylist: () => expected,
  playVideo: forbidden, pauseVideo: forbidden, seekTo: forbidden,
  loadPlaylist: forbidden, loadVideoById: forbidden,
};
const app = {polymerController: {
  getState: () => ({playerPage: {playerPageWatchNextResponse: {currentVideoEndpoint: endpoint}}}),
  dispatch: forbidden,
  handleQueueUpdateCommand(command) {
    commands.push(JSON.parse(JSON.stringify(command)));
    if (nativeFails) throw Error('temporary native failure');
  },
}};
const queue = {queue: {store: {store: {getState: () => ({queue: available ? {
  items: nativeIds.map((id, i) => {
    const item = {playlistPanelVideoRenderer: {videoId: id,
      navigationEndpoint: {watchEndpoint: {playlistId: nativePlaylist,
        ...(i === selected ? {index: savedIndex} : {})}}}};
    if (i === selected) return {playlistPanelVideoWrapperRenderer: {
      primaryRenderer: item, counterpart: scenario === 'malformed_counterpart' ? {contents: []} : [{counterpartRenderer: {
        playlistPanelVideoRenderer: {videoId: counterpart}}}],
    }};
    return i % 2 ? {playlistPanelVideoWrapperRenderer: {primaryRenderer: item}} : item;
  }), autoplay: false, selectedItemIndex: selected ?? nativeIds.indexOf(current),
  automixItems: scenario === 'malformed_automix' ? {contents: []} : undefined,
} : null})}}}};
const toggle = {
  id: 'automix', localName: 'tp-yt-paper-toggle-button', checked: true,
  textContent: '', getAttribute: () => null,
  click() { toggleClicks++; this.checked = false; },
};
const link = {
  href: 'https://music.youtube.com/playlist?list=PLSESSION',
  click() { clicks++; if (viewFails) throw Error('temporary view failure'); },
};
const rows = () => pageIds.map(id => ({data: {
  playlistItemData: {videoId: id},
  navigationEndpoint: {watchEndpoint: {videoId: id, playlistId: 'unrelated-row-link'}},
  overlay: {musicItemThumbnailOverlayRenderer: {content: {musicPlayButtonRenderer: {
    playNavigationEndpoint: {watchEndpoint: {videoId: id, playlistId: 'PLSESSION'}},
  }}}},
}}));
const window = {};
const context = vm.createContext({window, URL, AbortController, HTMLElement: class {},
  location: {href: 'https://music.youtube.com/playlist?list=PLSESSION',
    origin: 'https://music.youtube.com', reload: forbidden},
  Date: {now: () => now},
  document: {
    querySelector(selector) {
      return {'ytmusic-app': app, 'ytmusic-player-queue': queue, '#movie_player': api, video}[selector] || null;
    },
    querySelectorAll(selector) {
      if (selector.startsWith('ytmusic-playlist-shelf-renderer ')) return rows();
      if (selector.includes('a[href]')) return sidebarAvailable ? [link] : [];
      if (selector.includes('toggle-button')) return [toggle];
      return [];
    },
    addEventListener(type, callback, options) { listeners.push({type, callback, signal: options.signal}); },
  },
});
vm.runInContext(code, context);
const observer = window.__tcRadioSession;
const update = (refresh = true, ids = expected, removedCurrent) => observer.update({
  playlist: 'PLSESSION', videoIds: ids, refresh, removedCurrent,
});
const assertPlayback = () => {
  assert.equal(current, initialVideo);
  assert.equal(video.currentTime, 30);
  assert.equal(video.paused, false);
};

if (scenario === 'removed_current') {
  const hint = {videoId: current, afterIndex: 1};
  assert.equal(update().refresh, 'current song is not in saved playlist');
  assert.equal(update(true, expected, {...hint, afterIndex: -1}).playback_synced, false);
  nativePlaylist = 'OTHER';
  assert.equal(update(true, expected, hint).refresh, 'current song is not in saved playlist');
  nativePlaylist = playlist;
  endpoint.watchEndpoint.videoId = 'v2';
  assert.equal(update(true, expected, hint).refresh, 'waiting for current playback endpoint');
  assert.equal(commands.length, 0);
  endpoint.watchEndpoint.videoId = current;
  assert.equal(update(true, expected, hint).refresh, 'requested');
  const request = commands[0].queueUpdateCommand.fetchContentsCommand.watchEndpoint;
  assert.equal(request.videoId, 'retired');
  assert.equal(request.playlistId, playlist);
  assert(!('playlistSetVideoId' in request) && !('index' in request) && !('startTimeSeconds' in request));
  assert.equal(endpoint.watchEndpoint.playlistSetVideoId, 'set-v2');
  nativeIds = ['retired', 'v3', 'v4'];
  assert.equal(update(true, expected, hint).playback_synced, true);
  assert.equal(commands.length, 1);
} else if (scenario.startsWith('missing_row')) {
  const result = update();
  assert.equal(result.native.selectedItem, null);
  assert.equal(result.playback_synced, false);
  assert.equal(commands.length, scenario === 'missing_row' ? 1 : 0);
  if (scenario === 'missing_row') {
    assert.equal(result.refresh, 'requested');
    nativeIds = expected;
    assert.equal(update().savedIndex, 1);
    assert.equal(update().playback_synced, true);
  }
} else if (scenario === 'malformed_automix' || scenario === 'malformed_counterpart') {
  const result = update();
  assert.equal(result.native.selectedItem.videoId, current);
  assert.equal(result.savedIndex, 1);
  assert.equal(result.refresh, 'requested');
  assert.equal(commands.length, 1);
} else if (scenario === 'playability_identity') {
  const status = 'CONTENT_CHECK_REQUIRED';
  const response = {playabilityStatus: {status}, videoDetails: {videoId: current}};
  api.getPlayerResponse = () => response;
  const canRecover = () => observer.recoveryCurrent('PLSESSION', initialVideo);
  const watchUrl = 'https://music.youtube.com/watch?v=v2&list=PLSESSION';
  context.location.href = watchUrl;
  assert.equal(canRecover(), false, 'an already playing track must keep playing');
  video.paused = true;
  assert.equal(canRecover(), true);
  response.videoDetails.videoId = 'v1';
  assert.equal(canRecover(), false, 'a response naming a different video is stale');
  response.videoDetails.videoId = current;
  context.location.href = watchUrl.replace('v2', 'v3');
  assert.equal(canRecover(), false, 'a newly selected track may still be loading');
  context.location.href = watchUrl.replace('PLSESSION', 'OTHER');
  assert.equal(canRecover(), false, 'the same video can play in another playlist');
  context.location.href = 'https://music.youtube.com/playlist?list=OTHER';
  assert.equal(canRecover(), true, 'browsing keeps the loaded player and its endpoint');
  context.location.href = watchUrl;
  for (const ordinaryStatus of ['OK', undefined, '', {code: status}]) {
    response.playabilityStatus.status = ordinaryStatus;
    assert.equal(canRecover(), false, 'an ordinary pause needs no recovery');
  }
  video.paused = false;
} else if (scenario === 'counterpart_refresh') {
  assert.equal(update().refresh, 'requested');
  assert.equal(commands.length, 1);
  const watch = commands[0].queueUpdateCommand.fetchContentsCommand.watchEndpoint;
  assert.equal(watch.videoId, current, 'refresh retains the actual native upload');
  assert(!('index' in watch) && !('startTimeSeconds' in watch));
  nativeIds = expected;
  assert.equal(update().playback_synced, true);
  assert.equal(commands.length, 1);
} else if (['counterpart_confirmed', 'counterpart_trimmed', 'counterpart_repeat'].includes(scenario)) {
  assert.equal(update().playback_synced, true);
  assert.equal(commands.length, 0);
} else if (counterpart) {
  assert.equal(update().playback_synced, false);
  assert.equal(commands.length, 0, 'unproven, stale or mismatched slots cannot authorize a refresh');
} else if (scenario === 'native_then_page') {
  // A fresh browse page and the embedded API must not hide a stale native future.
  pageIds = expected;
  let result = update();
  assert.equal(result.page_synced, true);
  assert.equal(result.playback_synced, false);
  assert.equal(result.refresh, 'requested');
  assert.equal(clicks, 0);
  const {index, startTimeSeconds, ...watch} = endpoint.watchEndpoint;
  assert.deepEqual(commands, [{queueUpdateCommand: {fetchContentsCommand: {
    clickTrackingParams: 'tracking', watchEndpoint: watch,
  }}}]);
  assert.equal(endpoint.watchEndpoint.index, 7, 'the original native endpoint is immutable');
  nativeIds = expected;
  pageIds = ['v1', 'v2', 'old-next'];
  // Backend acknowledgement can stop refresh requests before the visible rows update.
  result = update(false);
  assert.equal(result.playback_synced, true);
  assert.equal(result.page_synced, false);
  assert.equal(result.viewRefresh, 'clicked');
  update(false);
  assert.equal(clicks, 1, 'click once per adopted saved version');
  pageIds = expected;
  assert.equal(update(false).page_synced, true);
  const nextVersion = [...expected, 'v5'];
  nativeIds = nextVersion;
  update(false, nextVersion);
  assert.equal(clicks, 2);
  assert.equal(commands.length, 1);
  assert.equal(toggleClicks, 1);
} else if (scenario === 'strict_future') {
  for (const ids of [
    ['v1', 'v2', 'v3'], ['v1', 'v2', 'v4', 'v3'],
    ['v1', 'v2', 'v3', 'v4', 'extra'], ['v1', 'v3', 'v4'],
  ]) {
    nativeIds = ids;
    assert.equal(update(false).playback_synced, false, JSON.stringify(ids));
  }
  nativeIds = expected;
  nativePlaylist = 'OTHER';
  assert.equal(update(false).playback_synced, false);
  nativePlaylist = playlist;
  available = false;
  assert.equal(update(false).playback_synced, false);
  available = true;
  // Only the future after the current song must match; history can be trimmed.
  nativeIds = ['v2', 'v3', 'v4'];
  assert.equal(update(false).playback_synced, true);
  assert.equal(commands.length, 0);
} else if (scenario === 'retries_and_drift') {
  update(); update();
  assert.equal(commands.length, 1);
  now += 7999; update();
  assert.equal(commands.length, 1);
  now += 1; update();
  now += 8000; update();
  now += 8000;
  assert.match(update().refresh, /not confirmed after 3 requests/);
  assert.equal(commands.length, 3);
  nativeIds = expected;
  assert.equal(update().refresh, 'confirmed');
  nativeIds = ['v1', 'v2', 'late-stale-next'];
  assert.equal(update().refresh, 'requested');
  assert.equal(commands.length, 4, 'confirmed adoption resets the drift repair budget');
} else if (scenario === 'endpoint_guard') {
  endpoint.watchEndpoint.videoId = 'v1';
  assert.match(update().refresh, /waiting for current playback endpoint/);
  endpoint.watchEndpoint.videoId = current;
  endpoint.watchEndpoint.playlistId = 'OTHER';
  update();
  assert.equal(commands.length, 0);
  endpoint.watchEndpoint.playlistId = playlist;
  assert.equal(update().refresh, 'requested');
} else if (scenario === 'other_playlist') {
  playlist = 'OTHER';
  const result = update();
  assert.equal(result.playback_synced, false);
  assert.equal(commands.length, 0);
  assert.equal(clicks, 0);
  assert.equal(toggleClicks, 0);
} else if (scenario === 'missing_sidebar') {
  nativeIds = expected;
  sidebarAvailable = false;
  assert.equal(update(false).viewRefresh, 'playlist link unavailable');
  sidebarAvailable = true;
  assert.equal(update(false).viewRefresh, 'clicked');
  update(false);
  assert.equal(clicks, 1);
} else if (scenario === 'refresh_errors') {
  nativeFails = true;
  assert.match(update().refreshRequest.error, /temporary native failure/);
  update();
  assert.equal(commands.length, 1);
  now += 8000;
  nativeFails = false;
  assert.equal(update().refresh, 'requested');
  nativeIds = expected;
  viewFails = true;
  assert.match(update(false).viewRefreshError, /temporary view failure/);
  update(false);
  assert.equal(clicks, 1, 'a failed view click cannot cause an infinite click loop');
} else if (scenario === 'reinjection') {
  vm.runInContext(code, context);
  assert.equal(window.__tcRadioSession, observer);
  assert.equal(window.__tcPlaylistPoc, observer);
  assert.equal(listeners.length, 2);
  let disposed = 0;
  window.__tcRadioSession = {version: -1, dispose() { observer.dispose(); disposed++; }};
  vm.runInContext(code, context);
  assert.equal(disposed, 1);
  assert.notEqual(window.__tcRadioSession, observer);
  assert(listeners.slice(0, 2).every(listener => listener.signal.aborted));
  assert(listeners.slice(2).every(listener => !listener.signal.aborted));
}
assertPlayback();
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=False, timeout=15)
    assert result.returncode == 0, result.stderr
