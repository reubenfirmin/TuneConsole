"""Keyboard shortcuts preserve native editing/activation; media controls target the exact upload."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_keyboard_and_media_commands_share_current_playback_without_duplicate_listeners():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required to exercise playback controls')
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const window = new EventTarget(), document = new EventTarget(), calls = [], handlers = {};
const session = {setActionHandler: (name,handler) => { handlers[name]=handler; }};
window.navigator = {mediaSession:session};
window.MediaMetadata = class {constructor(data) {Object.assign(this,data);}};
let refreshes = 0, releaseNext;
window.playbackStatus = {snapshot:{connected:true,now_playing:{video_id:'one',title:'Song',artist:'Artist',paused:false}},
  refresh: () => {refreshes++;}};
const context = vm.createContext({window,document,fetch: (url,options) => {
  calls.push({url,options});
  return url.endsWith('/next') ? new Promise(resolve => {releaseNext=resolve;}) : Promise.resolve({ok:true});
}, encodeURIComponent});
const source = fs.readFileSync('src/yt_playlist/web/static/playback-controls.js','utf8');
vm.runInContext(source,context); vm.runInContext(source,context);
const key = (properties={}) => {
  const event = new Event('keydown',{cancelable:true});
  const {target,...keys}=properties;
  Object.assign(event,{key:' ',code:'Space',...keys});
  if(target) Object.defineProperty(event,'target',{value:target});
  document.dispatchEvent(event); return event;
};
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  assert.equal(session.playbackState,'playing'); assert.equal(session.metadata.title,'Song');
  assert(key().defaultPrevented); await flush();
  assert.equal(calls.length,1,'script remount does not duplicate a Space command');
  assert.equal(calls[0].url,'/now-playing/toggle'); assert.equal(refreshes,1);
  for (const property of ['repeat','isComposing','altKey','ctrlKey','metaKey','shiftKey']) {
    assert(!key({[property]:true}).defaultPrevented);
  }
  assert(!key({key:'x',code:'KeyX'}).defaultPrevented);
  const prevented = new Event('keydown',{cancelable:true});
  Object.assign(prevented,{key:' ',code:'Space'}); prevented.preventDefault();
  document.dispatchEvent(prevented); assert.equal(calls.length,1);
  // Each interactive target uses its own native action (including a nested editable element).
  for (const selector of ['input','button','[contenteditable]','[role="slider"]','[role="checkbox"]']) {
    const target = new EventTarget(); target.closest = filter => {assert(filter.includes(selector)); return target;};
    const event = new Event('keydown',{cancelable:true}); Object.assign(event,{key:' ',code:'Space'});
    Object.defineProperty(event,'target',{value:target}); document.dispatchEvent(event);
    assert(!event.defaultPrevented);
  }
  assert.equal(calls.length,1);
  assert(key({key:'MediaPlayPause',code:''}).defaultPrevented); await flush();
  assert.equal(calls.length,2);
  handlers.play(); await flush(); assert.equal(calls.length,2,'play is idempotent while already playing');
  handlers.pause(); await flush(); assert.equal(calls.length,3);
  window.playbackStatus.snapshot.now_playing.paused = true;
  window.dispatchEvent(new Event('bridge-status')); assert.equal(session.playbackState,'paused');
  handlers.pause(); await flush(); assert.equal(calls.length,3);
  handlers.play(); await flush(); assert.equal(calls.length,4);
  handlers.nexttrack(); handlers.nexttrack(); await flush();
  assert.equal(calls.length,5,'duplicate media Next stays single while awaiting confirmation');
  assert.deepEqual(JSON.parse(calls.at(-1).options.body),{videoId:'one'});
  releaseNext({ok:true}); await flush();
  window.playbackStatus.snapshot = {connected:false,now_playing:{video_id:'old'}};
  window.dispatchEvent(new Event('bridge-status'));
  assert.equal(session.playbackState,'none'); assert.equal(session.metadata,null);
  assert(!key().defaultPrevented); handlers.nexttrack(); assert.equal(calls.length,5);
  window.playbackStatus.snapshot = {connected:true,now_playing:null};
  assert(!key().defaultPrevented);
  // The live card expired after a long pause, but its owned player remains resumable.
  window.playbackStatus.snapshot.resumable_player = {video_id:'paused/song', title:'Paused song', paused:true};
  window.dispatchEvent(new Event('bridge-status'));
  assert.equal(session.playbackState,'paused'); assert.equal(session.metadata.title,'Paused song');
  assert(key().defaultPrevented); await flush();
  assert.equal(calls.at(-1).url,'/now-playing/toggle?action=toggle&resume=true&videoId=paused%2Fsong');
  handlers.play(); await flush();
  assert.equal(calls.at(-1).url,'/now-playing/toggle?action=play&resume=true&videoId=paused%2Fsong');
  const resumedCalls=calls.length;
  handlers.pause(); handlers.nexttrack(); await flush();
  assert.equal(calls.length,resumedCalls,'expired display cannot skip and pause is idempotent');
  window.playbackStatus.snapshot.connected=false;
  assert(!key().defaultPrevented); handlers.play(); assert.equal(calls.length,resumedCalls);
  // Clicking a playback anchor leaves it focused, but anchors have no native Space action.
  window.playbackStatus.snapshot = {connected:true,now_playing:{video_id:'one',paused:false}};
  const anchor = {closest: filter => filter.split(',').some(part => part.trim() === 'a') ? anchor : null};
  assert(key({target:anchor}).defaultPrevented); await flush();
  assert.equal(calls.length,resumedCalls+1);
  // Browsers without Media Session retain the keyboard shortcut.
  const plainWindow = new EventTarget(); plainWindow.playbackStatus = window.playbackStatus;
  vm.runInContext(source,vm.createContext({window:plainWindow,document:new EventTarget(),fetch:()=>{},encodeURIComponent}));
})().catch(error => {console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node,'-e',script],cwd=Path(__file__).resolve().parents[1],
                            capture_output=True,text=True,timeout=20)
    assert result.returncode==0, result.stdout+result.stderr
