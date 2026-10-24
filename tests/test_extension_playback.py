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
      if(msg.type==='sensor-ping')return {ok:true,receiverRevision:7,mainVersion:4,mainRevision:8,mainAgeMs:0};
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
const {playback, tabs: musicTabs} = require('./tests/extension_worker.cjs').loadWorker(context);
sockets[0].readyState = Socket.OPEN;
const report = async (id, msg) => {
  for (const listener of runtime.onMessage.listeners) listener(msg, {tab: tabs.find(t => t.id === id)});
  await playback.drained;
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
    await report(2, {type: 'play', title: 'Song', paused: false, position:12, duration:200,
      timeZone: 'Pacific/Kiritimati'});
    if (scenario === 'leaving') {
      assert.equal(frames.length, 0, 'ignore playback from a tab navigating away from YouTube Music');
    } else {
      assert.equal(frames.at(-1).timeZone, 'Pacific/Kiritimati', 'preserve the content sensor timezone');
      assert.equal(frames.at(-1).position,12);
      assert.equal(frames.at(-1).duration,200);
    }
  }
  if (scenario === 'heartbeat') {
    await report(2, {type: 'now-heartbeat', title: 'Autoplay', paused: false, position:35, duration:200});
    assert.equal(frames.at(-1).position,35, 'the wire heartbeat retains its progress anchor');
    assert.equal(frames.at(-1).duration,200);
    const count = frames.length;
    await report(3, {type: 'now-heartbeat', title: 'Old playlist', paused: true});
    await report(3, {type: 'pevent', kind: 'bye'});
    assert.equal(frames.length, count, 'a paused/browsed tab cannot replace or clear Now Playing');
    await report(2, {type: 'now-heartbeat', title: 'Autoplay', paused: true});
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
    await playback.playPause();
    await playback.rate('like', 'v');
    await playback.next('v');
    assert.deepEqual(messages.filter(m=>m.type!=='sensor-ping').map(m => [m.id, m.type]),
      [[2, 'playpause'], [2, 'rate'], [2, 'next']]);
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
    await playback.playPause();
    assert.equal(messages.at(-1).id, 3);
    browsing.audible = false;
    await report(3, {type: 'now-heartbeat', title: 'Paused owner', paused: true});
    const pausedCount = frames.length;
    await report(2, {type: 'now-heartbeat', title: 'Muted old owner', paused: true});
    assert.equal(frames.length, pausedCount);
    assert.equal((await playback.pickTab()).id, 3);
    return;
  }
  if (['audible', 'heartbeat'].includes(scenario)) {
    await playback.playPause();
    await playback.rate('like', 'v');
    await sockets[0].onmessage({data:JSON.stringify({id:78,type:'next',videoId:'v'})});
    assert.deepEqual(messages.filter(m=>m.type!=='sensor-ping').map(m => [m.id, m.type]),
      [[2, 'playpause'], [2, 'rate'], [2, 'next']]);
    assert.equal(frames.at(-1).id,78);
    assert.equal(JSON.parse(frames.at(-1).body).ok,true);
  }
  if (scenario === 'pending') {
    const ready = musicTabs.ensure();
    await new Promise(resolve => setImmediate(resolve));
    const tab = tabs.find(t => t.id === 2);
    Object.assign(tab, {url: tab.pendingUrl, status: 'complete'});
    delete tab.pendingUrl;
    for (const listener of [...chrome.tabs.onUpdated.listeners]) listener(2, {status: 'complete'}, tab);
    assert.equal((await ready).id, 2);
  }
  if (scenario === 'concurrent') {
    await Promise.all([playback.navigate(next), playback.navigate(next + '_second')]);
    assert.equal(created.length, 1, 'overlapping launches create a single music tab');
    assert.equal(tabs.find(t => t.id === 10).url, next + '_second');
    return;
  }
  await playback.navigate(next);
  if (['no_music', 'leaving'].includes(scenario)) {
    assert.equal(created.length, 1);
    assert.equal(created[0].url, next);
    return;
  }
  assert.equal(created.length, 0, 'replace autoplay in its existing tab');
  assert.equal(tabs.find(t => t.id === 2).url, next);
  assert(updated.every(change => !change.active), 'keep the owner on TuneConsole');
  if (scenario !== 'pending') {
    assert(messages.some(m => m.id === 3 && m.type === 'pause'), 'silence the other music tab');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scenario", [
    "ready", "missing_sensor", "missing_receiver", "old_receiver", "newer_rating", "song_changed",
    "account_changed", "tab_closed", "tab_selection_changed", "uncertain_delivery", "expired", "legacy_toggle",
    "persistent_binding", "deadline_inflight", "old_exit_event",
])
def test_native_rating_recovers_only_explicitly_undispatched_desired_states(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the extension service worker")
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const scenario=process.argv[1],timers=new Map(),messages=[],injected=[],frames=[];
let serial=0,clock=1000,selected=2,release;
const context=vm.createContext({URL,Date:class extends Date{static now(){return clock;}},
  setTimeout(fn){const id=++serial;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
  isMissingReceiver:error=>error.message==='missing receiver',
  chrome:{tabs:{async sendMessage(id,message){
    if(message.type==='cancel-rating')return;
    messages.push({tabId:id,...message});
    if(messages.length===1){
      if(scenario==='missing_receiver')throw Error('missing receiver');
      return {ok:false,retryable:true,error:'No rating binding',diagnostics:{control:''}};
    }
    if(scenario==='uncertain_delivery')throw new Error('Reply channel closed');
    if(scenario==='deadline_inflight')return new Promise(resolve=>release=resolve);
    if(scenario==='song_changed')return {ok:false,error:'The playing song changed'};
    if(scenario==='persistent_binding')return {ok:false,retryable:true,error:'No rating binding',diagnostics:{control:''}};
    return {ok:true};
  }}},
});
vm.runInContext(fs.readFileSync('extension/worker/ratings.js','utf8'),context);
const playback=vm.runInContext('createNativeRatings',context)({
  pickTab:async()=>({id:selected}),
  sensors:{inject:async id=>injected.push(id),ping:async()=>{
    if(scenario==='old_receiver')return {ok:false,outdatedReceiver:true,error:'Outdated receiver'};
    return {ok:!(['missing_sensor','missing_receiver'].includes(scenario)&&!injected.length&&
      (scenario==='missing_sensor'||messages.length>0))};
  }},send:frame=>frames.push(frame),
});
(async()=>{
  const result=await playback.rate('like','A',scenario==='legacy_toggle'?undefined:'LIKE',false,'brand');
  if(['legacy_toggle','old_receiver'].includes(scenario)){
    assert.equal(result.ok,false);
    assert.equal(timers.size,0,'a toggle or obsolete receiver has no safe retry state');
    if(scenario==='old_receiver')assert.equal(messages.length,0,'never dispatch to an obsolete receiver');
    return;
  }
  assert.equal(result.ok,true);
  assert.equal(result.pending,true,'deferred native updates do not block the saved rating or skip');
  assert.equal(timers.size,1);
  const [timer,attempt]=[...timers.entries()][0];
  timers.delete(timer);
  if(scenario==='newer_rating'){
    await playback.rate('like','A','INDIFFERENT',false,'brand');
    await attempt();
    assert.equal(messages.length,2,'cancel an older retry even if its timer was already dispatched');
    assert.equal(messages.at(-1).likeStatus,'INDIFFERENT');
  }else if(['account_changed','tab_closed'].includes(scenario)){
    if(scenario==='account_changed')playback.observe({type:'now-heartbeat',videoId:'A',brandKnown:true,brandId:'other'},2);
    else playback.removed(2);
    await attempt();
    assert.equal(messages.length,1,'cancel without another delivery');
    assert.equal(frames.at(-1).state,'cancelled');
  }else if(scenario==='deadline_inflight'){
    const waiting=attempt();
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(typeof release,'function');
    clock+=31000;
    const [id,expire]=[...timers.entries()][0];timers.delete(id);expire();
    release({ok:true});await waiting;
    assert.equal(frames.at(-1).state,'failed','ignore a late success after the deadline');
  }else if(scenario==='persistent_binding'){
    await attempt();
    while(timers.size){
      const [id,fn]=[...timers.entries()][0];timers.delete(id);clock+=2000;await fn();
    }
    assert.equal(frames.length,2,'one pending and one terminal diagnostic despite repeated missing bindings');
    assert.equal(frames.at(-1).state,'failed');
    assert.match(frames.at(-1).error,/deadline expired/);
    assert.equal(frames.at(-1).first.diagnostics.control,'');
  }else{
    if(scenario==='expired')clock+=31000;
    if(scenario==='tab_selection_changed')selected=3;
    if(scenario==='old_exit_event')playback.observe({type:'pevent',kind:'track_exit',videoId:'preceding'},2);
    await attempt();
    assert.equal(messages.length,scenario==='expired'?1:2);
    assert(messages.every(message=>message.videoId==='A'&&message.brandId==='brand'&&message.likeStatus==='LIKE'));
    assert(messages.every(message=>message.tabId===2),'recovery stays in its original tab');
  }
  assert.equal(timers.size,0,'settled, changed, expired, or uncertain commands cannot keep retrying');
  assert.deepEqual(injected,['missing_sensor','missing_receiver'].includes(scenario)?[2]:[]);
  if(scenario!=='newer_rating'){
    assert.equal(frames.length,2);
    assert.equal(frames[0].state,'pending');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
