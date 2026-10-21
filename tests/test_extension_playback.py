"""Exercise playback ownership and playlist replacement with the real service worker."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", [
    "audible", "heartbeat", "pending", "concurrent", "no_music", "leaving", "tab_muted", "media_muted",
    "sole_tab_muted", "sole_media_muted", "sole_zero_volume", "selected_muted",
    "rating_handoff",
])
def test_playlist_launch_reuses_and_controls_the_playing_tab(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension service worker")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[1];
const event = () => ({listeners: [], addListener(fn) { this.listeners.push(fn); },
  removeListener(fn) { this.listeners = this.listeners.filter(listener => listener !== fn); }});
const app = {id: 1, url: 'http://127.0.0.1:8765/', active: true};
const old = {id: 2, url: 'https://music.youtube.com/watch?v=autoplay&list=RDAMVMautoplay',
             active: false, audible: ['audible', 'tab_muted', 'media_muted'].includes(scenario), lastAccessed: 1};
const browsing = {id: 3, url: 'https://music.youtube.com/', active: true, lastAccessed: 100};
if (scenario === 'tab_muted') Object.assign(browsing, {audible: true, mutedInfo: {muted: true}});
if (scenario === 'media_muted') browsing.audible = true;
let tabs = [app, old, browsing];
if (scenario.startsWith('sole_')) tabs=[app,old];
if (['sole_tab_muted','selected_muted'].includes(scenario)) old.mutedInfo={muted:true};
if (scenario==='selected_muted') {old.active=true;browsing.active=false;}
if (scenario === 'pending') {
  tabs = [app, {id: 2, url: 'about:blank', pendingUrl: old.url, status: 'loading'}];
}
if (['concurrent', 'no_music'].includes(scenario)) {
  tabs = [app, {id: 8, url: 'https://music.youtube.com.evil.example/'}];
}
if (scenario === 'leaving') tabs = [app, {...old, pendingUrl: 'https://example.org/'}];
const messages = [], frames = [], created = [], updated = [];
const runtime = {onMessage: event(), onStartup: event(), onInstalled: event()};
const sockets = [];
class Socket {
  static OPEN = 1;
  static CLOSED = 3;
  constructor() { this.readyState = 0; sockets.push(this); }
  send(frame) { frames.push(JSON.parse(frame)); }
}
const chrome = {
  runtime,
  tabs: {
    query: async ({url} = {}) => tabs.filter(t => !url || t.url.startsWith('https://music.youtube.com/')),
    get(id, callback) {
      const tab = tabs.find(t => t.id === id);
      if (callback) callback(tab);
      return Promise.resolve(tab);
    },
    update: async (id, changes) => {
      const tab = tabs.find(t => t.id === id);
      assert(tab);
      updated.push({id, ...changes});
      Object.assign(tab, changes);
      if (changes.url) delete tab.pendingUrl;
      return tab;
    },
    create: async properties => {
      created.push(properties);
      // Chrome has not committed the first document yet when another launch arrives.
      const tab = {id: 10, url: 'about:blank', pendingUrl: properties.url};
      tabs.push(tab);
      return tab;
    },
    sendMessage: async (id, msg) => {
      messages.push({id, ...msg});
      if(scenario==='rating_handoff' && msg.type==='rate')
        return {ok:msg.videoId===(id===2?'A':'B'),error:'The playing song changed'};
      return {ok: true};
    },
    onUpdated: event(), onRemoved: event(),
  },
  scripting: {executeScript: async () => {}},
  storage: {session: {get: async () => ({})}},
  windows: {onRemoved: event()},
  alarms: {create() {}, onAlarm: event()},
};
const context = vm.createContext({chrome, WebSocket: Socket, URL,
  console: {log() {}, warn() {}}, setTimeout() {}, clearTimeout() {},
});
vm.runInContext(fs.readFileSync('extension/background.js', 'utf8'), context);
sockets[0].readyState = Socket.OPEN;
const report = async (id, msg) => {
  for (const listener of runtime.onMessage.listeners) listener(msg, {tab: tabs.find(t => t.id === id)});
  await vm.runInContext('playbackRelay', context);
};
const next = 'https://music.youtube.com/watch?list=PL_new';
(async () => {
  if (scenario.startsWith('sole_') || scenario==='selected_muted') {
    const msg={type:'play',title:'Silent song',videoId:'silent',brandKnown:true,brandId:'brand',
      metadataVerified:true,paused:false,muted:scenario==='sole_media_muted'};
    if(scenario==='sole_zero_volume') msg.volume=0;
    await report(2,msg);
    assert.equal(frames.at(-1).type,'now-heartbeat');
    assert.equal(frames.filter(f=>f.type==='play').length,0);
    assert(frames.at(-1).muted || frames.at(-1).tabMuted);
    // A silent heartbeat must not consume the pending play.
    await report(2,{...msg,type:'now-heartbeat'});
    assert.equal(frames.filter(f=>f.type==='play').length,0);
    old.mutedInfo={muted:false};
    const audible={...msg,type:'now-heartbeat',muted:false,volume:1};
    await report(2,audible);
    assert.equal(frames.at(-1).type,'play');
    assert.equal(frames.at(-1).muted,false);
    assert.equal(frames.at(-1).tabMuted,false);
    await report(2,audible);
    assert.equal(frames.filter(f=>f.type==='play').length,1);
    return;
  }
  if (tabs.some(tab => tab.id === 2)) {
    await report(2, {type: 'play', title: 'Song', paused: false, timeZone: 'Pacific/Kiritimati'});
    if (scenario === 'leaving') {
      assert.equal(frames.length, 0, 'ignore playback from a tab navigating away from YouTube Music');
    } else {
      assert.equal(frames.at(-1).timeZone, 'Pacific/Kiritimati', 'preserve the content sensor timezone');
    }
  }
  if (scenario === 'heartbeat') {
    await report(2, {type: 'now-heartbeat', title: 'Autoplay', paused: false});
    const count = frames.length;
    await report(3, {type: 'now-heartbeat', title: 'Old playlist', paused: true});
    await report(3, {type: 'pevent', kind: 'bye'});
    assert.equal(frames.length, count, 'a paused/browsed tab cannot replace or clear Now Playing');
    await report(2, {type: 'now-heartbeat', title: 'Autoplay', paused: true});
    vm.runInContext('liveTabId = 2; standbyTabId = 3;', context);
    await report(3, {type: 'now-heartbeat', title: 'Standby', paused: false});
    assert.equal((await context.pickYtmTab()).id, 2, 'standby cannot take playback ownership');
  }
  if(scenario==='rating_handoff') {
    old.mutedInfo={muted:true};browsing.audible=true;
    await sockets[0].onmessage({data:JSON.stringify({id:77,type:'rate',action:'like',videoId:'A'})});
    assert.equal(messages.at(-1).id,3);
    assert.equal(messages.at(-1).videoId,'A','handoff must not retarget the displayed song');
    assert.equal(frames.at(-1).id,77);
    assert.equal(JSON.parse(frames.at(-1).body).ok,false);
    return;
  }
  if (['tab_muted', 'media_muted'].includes(scenario)) {
    const count = frames.length;
    for (const type of ['play', 'now-heartbeat']) {
      await report(3, {type, title: 'Muted song', videoId: 'muted-video', brandKnown: true,
        brandId: 'brand-A', paused: false, muted: scenario === 'media_muted', timeZone: 'America/Costa_Rica'});
    }
    await report(3, {type: 'pevent', kind: 'state', state: 'playing'});
    assert.equal(frames.length, count, 'muted playback must not replace the audible card');
    await context.playPauseInYtmTab();
    await context.rateInYtmTab('like', 'v');
    assert.deepEqual(messages.map(m => [m.id, m.type]), [[2, 'playpause'], [2, 'rate']]);
    assert.equal(messages.at(-1).videoId,'v');
    // A real ownership change follows current mute state, then preserves that owner while paused.
    old.mutedInfo = {muted: true};
    browsing.mutedInfo = {muted: false};
    const beforeVerified = frames.filter(frame => frame.type === 'play').length;
    await report(3, {type: 'now-heartbeat', title: 'Unverified player title', artist: 'Uploader',
      videoId: 'muted-video', metadataVerified: false,
      brandKnown: true, brandId: 'brand-A', paused: false, muted: false});
    assert.equal(frames.at(-1).type, 'now-heartbeat', 'unverified credits must not promote a pending play');
    assert.equal(frames.filter(frame => frame.type === 'play').length, beforeVerified);
    await report(3, {type: 'now-heartbeat', title: 'Audible now', videoId: 'muted-video',
      brandKnown: true, brandId: 'brand-A', paused: false, muted: false});
    assert.equal(frames.at(-1).title, 'Audible now');
    assert.equal(frames.at(-1).type, 'play', 'record the previously rejected play once it owns playback');
    assert.equal(frames.at(-1).brandId, 'brand-A');
    assert.equal(frames.at(-1).timeZone, 'America/Costa_Rica');
    const playCount = frames.filter(frame => frame.type === 'play').length;
    await report(3, {type: 'now-heartbeat', title: 'Audible now', videoId: 'muted-video',
      brandKnown: true, brandId: 'brand-A', paused: false, muted: false});
    assert.equal(frames.at(-1).type, 'now-heartbeat');
    assert.equal(frames.filter(frame => frame.type === 'play').length, playCount);
    await context.playPauseInYtmTab();
    assert.equal(messages.at(-1).id, 3);
    browsing.audible = false;
    await report(3, {type: 'now-heartbeat', title: 'Paused owner', paused: true});
    const pausedCount = frames.length;
    await report(2, {type: 'now-heartbeat', title: 'Muted old owner', paused: true});
    assert.equal(frames.length, pausedCount);
    assert.equal((await context.pickYtmTab()).id, 3);
    return;
  }
  if (['audible', 'heartbeat'].includes(scenario)) {
    await context.playPauseInYtmTab();
    await context.rateInYtmTab('like', 'v');
    assert.deepEqual(messages.map(m => [m.id, m.type]), [[2, 'playpause'], [2, 'rate']]);
  }
  if (scenario === 'pending') {
    const ready = context.ensureYtmTab();
    await new Promise(resolve => setImmediate(resolve));
    const tab = tabs.find(t => t.id === 2);
    Object.assign(tab, {url: tab.pendingUrl, status: 'complete'});
    delete tab.pendingUrl;
    for (const listener of [...chrome.tabs.onUpdated.listeners]) listener(2, {status: 'complete'}, tab);
    assert.equal((await ready).id, 2);
  }
  if (scenario === 'concurrent') {
    await Promise.all([context.navigateYtmTab(next), context.navigateYtmTab(next + '_second')]);
    assert.equal(created.length, 1, 'overlapping launches create a single music tab');
    assert.equal(tabs.find(t => t.id === 10).url, next + '_second');
    return;
  }
  await context.navigateYtmTab(next);
  if (['no_music', 'leaving'].includes(scenario)) {
    assert.equal(created.length, 1);
    assert.equal(created[0].url, next);
    return;
  }
  assert.equal(created.length, 0, 'replace autoplay in its existing tab');
  assert.equal(tabs.find(t => t.id === 2).url, next);
  assert(updated.every(change => !change.active), 'keep the owner on TuneConsole');
  if (scenario !== 'pending') {
    assert(messages.some(m => m.id === 3 && m.type === 'deck-pause'), 'silence the other music tab');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
