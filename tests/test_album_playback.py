"""Album buttons reuse global commands and only pause their own playback context."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_album_controls_match_queue_resume_and_upload_handoff_without_polling():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required to exercise album playback controls')
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const calls = [];
const window = {playbackStatus:{snapshot:null},
  playbackControls:{command:action => {calls.push(action); return true;}},
  tcPlay:url => calls.push(url)};
const context = vm.createContext({window,URL,location:new URL('http://localhost/'),
  htmx:{trigger:(button,event) => calls.push(event)}});
vm.runInContext(fs.readFileSync('src/yt_playlist/web/static/album-player.js','utf8'),context);
const button = context.albumPlayback('album', 'album-queue');
button.init(); assert.equal(button.playing,false);
const song = {video_id:'one',paused:false,playlist_ytm_id:'album-queue'};
button.apply({connected:true,now_playing:song});
assert.equal(button.playing,true); button.play({}); assert.deepEqual(calls,['toggle']);
button.apply({connected:true,resumable_player:{...song,paused:true}});
assert.equal(button.active,true); assert.equal(button.playing,false);
button.play({}); assert.deepEqual(calls,['toggle','toggle']);
// Matching album metadata cannot override the actual queue.
button.apply({connected:true,now_playing:{...song,playlist_ytm_id:'party',
  source:{kind:'album',url:'/album?browse=album'}}});
assert.equal(button.active,false); button.play({}); assert.equal(calls.at(-1),'album-start');
button.apply({connected:false,now_playing:song}); assert.equal(button.active,false);
const held = context.albumPlayback('album');
held.apply({connected:true,now_playing:{...song,source:{kind:'album',url:'/album?browse=album'}}});
assert.equal(held.playing,true); held.play({}); assert.equal(calls.at(-1),'toggle');
held.apply({connected:true,now_playing:{...song,source:{kind:'playlist',url:'/playlist/1'}}});
assert.equal(held.active,false);
const upload = context.albumPlayback('private', '', 'https://music.youtube.com/browse/private');
upload.init(); upload.play({}); assert.equal(calls.at(-1),'https://music.youtube.com/browse/private');
"""
    result = subprocess.run([node, '-e', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
