"""A newer audio intent revokes pending TV work without destroying its player."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('pending_work', ['takeover', 'resume', 'select', 'retry', 'ready', 'api', 'start', 'open'])
@pytest.mark.parametrize('rejected', [False, True])
def test_audio_release_revokes_pending_tv_work_and_allows_explicit_resume(pending_work, rejected):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for JS behavior checks')
    script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const [work, rejected] = process.argv.slice(1);
const pending = [], calls = [];
let mounted, scriptTag;
const flush = () => new Promise(setImmediate);
global.window = {dispatchEvent() {}};
global.document = {
  body: {classList: {toggle() {}}},
  createElement() { return {remove() {}}; },
  head: {append(script) { scriptTag = script; }},
};
global.location = {origin: 'http://127.0.0.1'};
const api = {Player: class {
  constructor(mount, options) {
    this.options = options; this.id = options.videoId; mounted = this;
    if (work !== 'ready') queueMicrotask(() => options.events.onReady({target: this}));
  }
  playVideo() { calls.push(['play', this.id]); this.options.events.onStateChange({data: 1, target: this}); }
  pauseVideo() { calls.push(['pause', this.id]); this.options.events.onStateChange({data: 2, target: this}); }
  loadVideoById(id) { this.id = id; calls.push(['load', id]); this.playVideo(); }
  destroy() { calls.push(['destroy']); }
}};
if (work !== 'api') window.YT = api;
global.fetch = url => url.endsWith('/refill') ? Promise.resolve({status: 202, json: async () => ({preparing: true})}) :
  url.endsWith('/event') ? Promise.resolve({ok: true}) :
  new Promise(resolve => pending.push({url, resolve}));
function respond(payload, ok = true) {
  assert.equal(pending.length, 1, 'one actual pending request');
  pending.shift().resolve({ok, json: async () => payload});
}
vm.runInThisContext(fs.readFileSync('src/yt_playlist/web/static/tv.js', 'utf8'));
const model = tvHome();
model.$refs = {player: {replaceChildren() {}}}; model.$nextTick = () => {};
global.ResizeObserver = class { observe() {} disconnect() {} };
Object.assign(window, {addEventListener() {}, removeEventListener() {}});
model.init(); model.visible = true;
const playlist = {items: [{video_id: 'video-a'}, {video_id: 'video-b'}], playlist_id: 1, title: 'TV'};
let operation;
try {
  if (work === 'start' || work === 'open') {
    operation = work === 'start' ? model.start() : model.openPlaylist(1);
  } else {
    model.usePlaylist(playlist);
    operation = model.select(0);
    await flush();
    if (work === 'resume' || work === 'select' || work === 'retry') {
      respond({ok: true}); await operation; await flush();
      calls.length = 0;
      if (work === 'retry') mounted.options.events.onError({data: 5, target: mounted});
      else operation = work === 'resume' ? model.resume() : model.select(1);
    }
  }
  window.tvPlaybackControls.release();
  if (work === 'ready') {
    mounted.options.events.onReady({target: mounted});
  } else if (work === 'api') {
    if (rejected === 'true') scriptTag.onerror();
    else { window.YT = api; window.onYouTubeIframeAPIReady(); }
  } else {
    respond(rejected === 'true' ? {error: 'Old request failed'} :
      work === 'start' || work === 'open' ? playlist : {ok: true}, rejected !== 'true');
  }
  await operation; await flush();
  assert.equal(calls.some(([action]) => action === 'play' || action === 'load'), false,
    'older work must not restart TV after audio takes ownership');
  assert.equal(pending.length, 0, 'late player readiness must not request takeover');
  assert.equal(window.tvPlaybackControls.active(), false);
  assert.equal(model.error, ''); assert.equal(model.playerError, '');
  assert.equal(model.starting, false); assert.equal(model.connecting, false); assert.equal(model.loading, false);
  if (work === 'start' || work === 'open') {
    assert.equal(model.playlistId, null, 'late creation/open must not replace the current playlist');
    assert.equal(mounted, undefined);
    // A fresh Play request still works, even if it overlaps the old creation.
    operation = model.start(); respond(playlist); await operation; await flush();
  } else if (work === 'api') {
    window.YT = api; operation = model.select(0); await operation; await flush();
  } else {
    assert.equal(calls.some(([action]) => action === 'destroy'), false,
      'release must preserve the mounted player and its callbacks');
    operation = model.resume();
  }
  respond({ok: true}); await operation; await flush();
  assert.equal(window.tvPlaybackControls.active(), true);
  assert.equal(model.playing, true, 'the retained state callback must still work');
  assert.equal(mounted.id, model.current.video_id, 'resuming a cancelled selection loads the selected video');
  assert.equal(calls.filter(([action]) => action === 'play').length, 1);
} finally { model.destroy(); }
'''
    result = subprocess.run(
        [node, '--input-type=module', '-e', script, pending_work, str(rejected).lower()],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_delayed_player_events_cannot_reclaim_audio_but_native_play_after_pause_can():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for JS behavior checks')
    script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const pending = [], calls = [];
let mounted;
const flush = () => new Promise(setImmediate);
global.window = {dispatchEvent() {}, YT: {Player: class {
  constructor(mount, options) {
    this.options = options; mounted = this;
    queueMicrotask(() => options.events.onReady({target: this}));
  }
  playVideo() { calls.push('play'); }
  pauseVideo() { calls.push('pause'); }
  destroy() {}
}}};
global.document = {body: {classList: {toggle() {}}}, createElement() { return {}; }};
global.location = {origin: 'http://127.0.0.1'};
global.fetch = url => url.endsWith('/refill') ? Promise.resolve({status: 202, json: async () => ({preparing: true})}) :
  url.endsWith('/event') ? Promise.resolve({ok: true}) :
  new Promise(resolve => pending.push(resolve));
vm.runInThisContext(fs.readFileSync('src/yt_playlist/web/static/tv.js', 'utf8'));
const model = tvHome();
model.$refs = {player: {replaceChildren() {}}}; model.$nextTick = () => {};
global.ResizeObserver = class { observe() {} disconnect() {} };
Object.assign(window, {addEventListener() {}, removeEventListener() {}});
model.init(); model.visible = true;
model.items = [{video_id: 'video-a'}, {video_id: 'video-b'}];
const state = data => mounted.options.events.onStateChange({data, target: mounted});
try {
  await model.select(0); await flush();
  pending.shift()({ok: true, json: async () => ({ok: true})}); await flush();
  assert.deepEqual(calls, ['play']);
  model.externalPlayback();
  state(1); state(0); await flush(); // delayed PLAYING / ENDED from the old play
  assert.equal(pending.length, 0, 'old events cannot request a new takeover');
  assert.equal(calls.filter(action => action === 'play').length, 1);
  assert.equal(model.playing, false); assert.equal(model.selected, 0);
  assert.equal(window.tvPlaybackControls.active(), false);
  state(2); // the release pause is now acknowledged
  state(1); await flush(); // a subsequent native iframe Play
  assert.equal(pending.length, 1, 'native Play still requests confirmed audio pause');
  assert.equal(calls.filter(action => action === 'play').length, 1);
  pending.shift()({ok: true, json: async () => ({ok: true})}); await flush();
  assert.equal(calls.filter(action => action === 'play').length, 2);
  state(1);
  assert.equal(model.playing, true); assert.equal(window.tvPlaybackControls.active(), true);
} finally { model.destroy(); }
'''
    result = subprocess.run([node, '--input-type=module', '-e', script],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_already_paused_native_play_reacquires_without_an_extra_pause_acknowledgement():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for JS behavior checks')
    script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const pending = [], calls = [];
let mounted;
const flush = () => new Promise(setImmediate);
global.window = {dispatchEvent() {}, YT: {Player: class {
  constructor(mount, options) {
    this.options = options; this.state = 2; mounted = this;
    queueMicrotask(() => options.events.onReady({target: this}));
  }
  playVideo() { calls.push('play'); }
  pauseVideo() { calls.push('pause'); } // already paused: no new callback
  getPlayerState() { return this.state; }
  destroy() {}
}}};
global.document = {body: {classList: {toggle() {}}}, createElement() { return {}; }};
global.location = {origin: 'http://127.0.0.1'};
global.fetch = url => url.endsWith('/event') ? Promise.resolve({ok: true}) :
  new Promise(resolve => pending.push(resolve));
vm.runInThisContext(fs.readFileSync('src/yt_playlist/web/static/tv.js', 'utf8'));
const model = tvHome();
model.$refs = {player: {replaceChildren() {}}}; model.$nextTick = () => {};
global.ResizeObserver = class { observe() {} disconnect() {} };
Object.assign(window, {addEventListener() {}, removeEventListener() {}});
model.init(); model.visible = true; model.items = [{video_id: 'video-a'}];
try {
  await model.select(0); await flush();
  pending.shift()({ok: true, json: async () => ({ok: true})}); await flush();
  mounted.options.events.onStateChange({data: 2, target: mounted});
  model.externalPlayback(); calls.length = 0;
  mounted.state = 1;
  mounted.options.events.onStateChange({data: 1, target: mounted}); await flush();
  assert.equal(pending.length, 1, 'first genuine native Play must request audio handoff');
  pending.shift()({ok: true, json: async () => ({ok: true})}); await flush();
  assert.equal(calls.filter(call => call === 'play').length, 1);
  assert.equal(window.tvPlaybackControls.active(), true);
} finally { model.destroy(); }
'''
    result = subprocess.run([node, '--input-type=module', '-e', script],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_audio_reclaims_tv_only_after_the_confirmed_takeover():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for JS behavior checks')
    script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
let now = 90, respond, mounted;
const calls = [], flush = () => new Promise(setImmediate);
global.window = new EventTarget();
window.performance = {now: () => now};
global.document = new EventTarget();
Object.assign(document, {readyState: 'loading', getElementById: () => null,
  body: {classList: {toggle() {}}}, createElement: () => ({})});
global.location = {origin: 'http://localhost'};
global.ResizeObserver = class { observe() {} disconnect() {} };
global.requestAnimationFrame = () => 1;
global.cancelAnimationFrame = () => {};
global.fetch = url => url === '/bridge/status' ? new Promise(resolve => { respond = resolve; }) :
  Promise.resolve({ok: true, status: 202, json: async () => ({ok: true, preparing: true})});
window.YT = {Player: class {
  constructor(mount, options) {
    this.options = options; this.state = 2; mounted = this;
    queueMicrotask(() => options.events.onReady({target: this}));
  }
  getPlayerState() { return this.state; }
  getCurrentTime() { return 37; }
  getDuration() { return 200; }
  playVideo() { this.state = 1; calls.push('play'); this.options.events.onStateChange({data: 1, target: this}); }
  pauseVideo() { this.state = 2; calls.push('pause'); this.options.events.onStateChange({data: 2, target: this}); }
  destroy() {}
}};
const root = 'src/yt_playlist/web/static/';
vm.runInThisContext(fs.readFileSync(root + 'playback-status.js', 'utf8'));
vm.runInThisContext(fs.readFileSync(root + 'tv.js', 'utf8'));
const model = tvHome();
model.$refs = {player: {replaceChildren() {}}}; model.$nextTick = () => {};
model.init(); model.visible = true; model.items = [{video_id: 'tv-song', title: 'TV song'}];
const owner = window.playbackStatus;
const audio = {video_id: 'audio-song', title: 'Audio song', paused: false, position: 12,
  duration: 180, snapshot_age: 0, source: {kind: 'playlist', url: '/playlist/1'}};
const data = changes => ({connected: true, radio: true, radio_ready: true,
  now_playing: {...audio, ...changes}, radio_resume: {video_id: 'saved-radio'}});
async function read(changes) {
  const pending = owner.refresh(); respond({ok: true, json: async () => data(changes)}); await pending;
}
function tvOwns() {
  assert.equal(window.tvPlaybackControls.active(), true);
  assert.equal(owner.snapshot.now_playing.video_id, 'tv-song');
  assert.equal(owner.snapshot.now_playing.paused, false);
  assert.equal(owner.snapshot.now_playing.source.kind, 'tv');
  assert.equal(owner.snapshot.radio, false); assert.equal(owner.snapshot.radio_ready, false);
  assert.equal(owner.snapshot.radio_resume, null);
}
try {
  const staleRead = owner.refresh(); // This read starts before audio is paused for TV.
  now = 100; await model.select(0); await flush();
  now = 300; respond({ok: true, json: async () => data({})}); await staleRead;
  tvOwns();
  assert.equal(owner.audioSnapshot.now_playing.video_id, 'audio-song');
  now = 400; await read({snapshot_age: .5}); tvOwns();
  for (const changes of [{paused: true}, {muted: true}, {tab_muted: true}, {volume: 0},
      {snapshot_age: -1}, {snapshot_age: null}, {snapshot_age: '0'}]) {
    now += 10; await read(changes); tvOwns();
  }
  // Buffering keeps Pause actionable but does not invent advancing progress.
  mounted.state = 3; mounted.options.events.onStateChange({data: 3, target: mounted});
  tvOwns();
  const position = owner.progress().position;
  now += 200; assert.equal(owner.progress().position, position);
  mounted.state = 1; mounted.options.events.onStateChange({data: 1, target: mounted});
  now = 1000; await read({});
  assert.equal(window.tvPlaybackControls.active(), false);
  assert.equal(mounted.state, 2);
  assert.deepEqual(calls, ['play', 'pause']);
  assert.equal(owner.snapshot.now_playing.video_id, 'audio-song');
  assert.equal(owner.snapshot.now_playing.source.kind, 'playlist');
  assert.equal(owner.progress().videoId, 'audio-song');
  assert.equal(owner.progress().position, 12);
  now = 1100; await model.resume(); tvOwns();
  // Re-publishing that cached audio observation after a new TV Play cannot steal it.
  model.publish(); tvOwns();
  now = 1200; await read({snapshot_age: .2}); tvOwns();
  const failed = owner.refresh(); respond({ok: false}); await failed; tvOwns();
  assert.deepEqual(calls, ['play', 'pause', 'play']);
} finally { model.destroy(); owner.stop(); }
'''
    result = subprocess.run([node, '--input-type=module', '-e', script],
                            cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
