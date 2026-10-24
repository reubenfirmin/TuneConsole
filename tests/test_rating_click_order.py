"""Rapid player clicks keep their intent order before dispatch and across failures."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_player_serializes_rapid_choices_without_stale_rollback():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const player = fs.readFileSync('src/yt_playlist/web/static/player-card.js', 'utf8');
const calls = [], window = new EventTarget();
let refreshes = 0;
window.playbackStatus = {refresh: () => {refreshes++;}, progress: () => null};
const context = vm.createContext({window, location: new URL('http://localhost/'), URL, CustomEvent,
  document: {querySelector: () => null}, console,
  fetch: (url, options) => new Promise((resolve, reject) => calls.push({
    body: JSON.parse(options.body), resolve: data => resolve({json: async () => data}), reject}))});
vm.runInContext(player, context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  const player = vm.runInContext('playerStatus()', context);
  const snapshot = status => ({connected: true, now_playing: {video_id: 'song', likeStatus: status}});
  player.apply(snapshot('INDIFFERENT'));
  const first = player.rate('like');
  const last = player.rate('like');
  assert.equal(player.nowPlaying.likeStatus, 'INDIFFERENT');
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.likeStatus, 'LIKE');
  player.apply(snapshot('LIKE'));
  assert.equal(player.nowPlaying.likeStatus, 'INDIFFERENT', 'older confirmation cannot replace queued intent');
  calls[0].resolve({ok: true});
  await first; await flush();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].body.likeStatus, 'INDIFFERENT');
  calls[1].resolve({ok: true}); await last;
  player.apply(snapshot('INDIFFERENT'));
  assert.equal(player.nowPlaying.likeStatus, 'INDIFFERENT');
  const rejectedFirst = player.rate('like'), rejectedLast = player.rate('like');
  await flush(); calls[2].resolve({ok: false}); await rejectedFirst; await flush();
  calls[3].resolve({ok: false}); await rejectedLast;
  assert.equal(player.nowPlaying.likeStatus, 'INDIFFERENT', 'two failures cannot restore an unconfirmed like');
  const interrupted = player.rate('like'); await flush();
  calls[4].reject(Error('response lost')); await interrupted;
  assert.equal(player.pendingRating, null);
  assert.equal(player.nowPlaying.likeStatus, 'LIKE', 'lost reply does not prove cancellation');
  assert.equal(refreshes, 5);
  const dislike = player.rate('dislike');
  player.rate('dislike'); await flush();
  assert.equal(calls.length, 6, 'dislike/skip cannot be issued twice while pending');
  assert.equal(calls[5].body.videoId, 'song');
  calls[5].resolve({ok: true}); await dislike;
})();
"""
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).parents[1],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
