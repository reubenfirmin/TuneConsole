"""The record follows track artwork immediately without mistaking history for playback."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_record_uses_each_track_cover_and_reconciles_playback_requests():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the radio record")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const window = new EventTarget();
let now = 1000, resolvePlay;
const context = vm.createContext({window, URL, CustomEvent, console,
  Date: class extends Date { static now() { return now; } },
  location: new URL('http://localhost/#radio'), sessionStorage: {getItem: () => null},
  document: {getElementById: () => null},
  fetch: () => new Promise(resolve => { resolvePlay = resolve; }),
});
for (const file of ['radio-artwork.js', 'radio.js'])
  vm.runInContext(fs.readFileSync('src/yt_playlist/web/static/' + file, 'utf8'), context);
const radio = vm.runInContext('radioHome()', context);
const tracks = [
  {video_id: 'one', thumbnail: 'https://yt3.googleusercontent.com/track-specific=w120-h120-l90-rj'},
  {video_id: 'two', thumbnail: 'https://art.example/another-track.jpg'},
  {video_id: 'three', thumbnail: 'https://art.example/album.jpg'},
];
radio.state = {active: true, playlist_id: 'PLRADIO', tracks, history: tracks, history_count: 3, position: 0};
assert.equal(radio.recordTrack.video_id, 'one', 'current position takes priority over the last history row');
assert.equal(radio.recordCover, 'https://yt3.googleusercontent.com/track-specific=w544-h544-l90-rj');
const status = video => ({connected: true, now_playing: {video_id: video,
  playlist_ytm_id: 'PLRADIO', thumbnail: 'https://art.example/player.jpg'}});
radio.syncRecordPlayback(status('three'));
assert.equal(radio.recordCover, tracks[2].thumbnail);
window.addEventListener('playback-requested', e => radio.previewRecord(e.detail));
window.addEventListener('playback-settled', e => radio.settleRecord(e.detail));
const app = fs.readFileSync('src/yt_playlist/web/static/app.js', 'utf8');
vm.runInContext(app.slice(app.indexOf('function tcPlay('), app.indexOf('// Capture phase')), context);
(async () => {
  const requested = context.tcPlay('https://music.youtube.com/watch?v=two&list=PLRADIO');
  assert.equal(radio.recordCover, tracks[1].thumbnail, 'cover changes before the play request resolves');
  radio.syncRecordPlayback(status('three'));
  assert.equal(radio.recordCover, tracks[1].thumbnail, 'a preceding heartbeat cannot undo the click');
  resolvePlay({json: async () => ({ok: true})});
  await requested;
  radio.syncRecordPlayback(status('two'));
  assert.equal(radio.pendingRecord, null);
  assert.equal(radio.recordCover, tracks[1].thumbnail);
  radio.syncRecordPlayback(status('one'));
  assert.equal(radio.recordTrack.video_id, 'one', 'native backward playback updates without a table replacement');
  assert.equal(radio.state.history_count, 3);
  radio.currentVideo = 'three';
  assert.equal(radio.recordTrack.video_id, 'one', 'an older radio snapshot cannot replace the shared playback snapshot');
  radio.previewRecord({url: 'https://music.youtube.com/watch?v=three&list=PLRADIO', requestId: 10});
  radio.previewRecord({url: 'https://music.youtube.com/watch?v=two&list=PLRADIO', requestId: 11});
  radio.syncRecordPlayback(status('one'));
  assert.equal(radio.recordCover, tracks[1].thumbnail, 'rapid clicks retain their confirmed starting point');
  radio.settleRecord({requestId: 10, ok: false});
  assert.equal(radio.recordCover, tracks[1].thumbnail, 'an older failed request cannot revert a newer click');
  radio.settleRecord({requestId: 11, ok: false});
  assert.equal(radio.recordTrack.video_id, 'one', 'failed playback restores the confirmed cover');
  radio.previewRecord({url: 'https://music.youtube.com/watch?v=three&list=PLRADIO', requestId: 12});
  now += 8001;
  assert.equal(radio.recordTrack.video_id, 'one', 'unconfirmed navigation has a bounded preview');
  radio.previewRecord({url: 'https://music.youtube.com/watch?v=two&list=OTHER', requestId: 13});
  assert.equal(radio.recordTrack.video_id, 'one', 'another playlist cannot replace the radio cover');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
