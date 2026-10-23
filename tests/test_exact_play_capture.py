"""Exercise Music's missing-bar-data layout through the real sensor, relay and ingestion."""
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from yt_playlist.library.live_plays import handle_play_event


@pytest.mark.parametrize("scenario", [
    "organic", "generated", "wrong_title", "wrong_artist", "missing_artist", "stale_target",
    "conflicting_targets", "conflicting_bar", "unknown_account", "ambiguous_account",
    "paused", "muted", "tab_muted",
])
def test_missing_bar_identity_requires_coherent_player_credits_before_exact_capture(store, scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the real extension sensor and relay")
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const scenario=process.argv[1],frames=[],sockets=[],queued=[],handlers=[],polls=[];
const event=()=>({addListener(){},removeListener(){}});
const tab={id:1,url:'https://music.youtube.com/',audible:true,active:true,
  mutedInfo:{muted:scenario==='tab_muted'}};
class Socket {
  static OPEN=1;static CLOSED=3;
  constructor(){this.readyState=0;sockets.push(this);}
  send(frame){frames.push(JSON.parse(frame));}
}
const workerContext=vm.createContext({URL,WebSocket:Socket,
  chrome:{runtime:{onMessage:event(),onStartup:event(),onInstalled:event()},
    tabs:{query:async()=>[tab],get:async()=>tab,onUpdated:event(),onRemoved:event()},
    windows:{onRemoved:event()},alarms:{create(){},onAlarm:event()}},
  console:{log(){},warn(){}},setTimeout(){},clearTimeout(){}});
const worker=require('./tests/extension_worker.cjs').loadWorker(workerContext);
sockets[0].readyState=Socket.OPEN;
let current='current',runtime;
const video={paused:scenario==='paused',muted:scenario==='muted',volume:1,
  currentTime:12,duration:200,addEventListener(){},removeEventListener(){}};
const title=()=>current==='current'?'Song':'Next song';
const target=()=>scenario==='stale_target'?'previous':current;
const window={ytcfg:{get:key=>scenario==='unknown_account'?undefined:
  key==='LOGGED_IN'?true:'brand-A'},
  addEventListener(type,fn){if(type==='message')handlers.push(fn);},
  removeEventListener(){},postMessage(msg){queued.push(msg);}};
const document={addEventListener(){},querySelector(selector){
  if(selector==='video')return video;
  if(selector==='#movie_player')return {
    getVideoData:()=>({video_id:current,title:title(),author:'Artist'}),
    getPlaylistId:()=> 'PL_current'};
  // This is the observed live layout: no element/controller data and no watch link.
  if(selector==='ytmusic-player-bar')return {getAttribute:()=>''};
  if(selector==='.title.ytmusic-player-bar')return {textContent:scenario==='wrong_title'?'Previous song':title()};
  if(selector==='.byline.ytmusic-player-bar')return {
    textContent:scenario==='missing_artist'?'':scenario==='wrong_artist'?'Previous artist':'Artist'};
  if(selector==='img.image.ytmusic-player-bar')return {src:scenario==='conflicting_bar'
    ?'https://i.ytimg.com/vi/previous/hqdefault.jpg':'https://lh3.googleusercontent.com/album'};
  if(selector==='ytmusic-player-bar ytmusic-like-button-renderer')return {
    data:{target:{videoId:target()}},polymerController:{data:{target:{
      videoId:scenario==='conflicting_targets'?'previous':target()}}},
    getAttribute:()=> 'INDIFFERENT',querySelector:()=>null};
  return null;
}};
const pageContext=vm.createContext({window,document,navigator:{mediaSession:{metadata:null}},URL,Intl,
  location:{href:'https://music.youtube.com/watch?v=browsed&list=PL_browsed'},
  chrome:{runtime:{onMessage:{addListener(fn){runtime=fn;}},
    sendMessage(msg){worker.playback.receive(msg,{tab});}}},
  console:{log(){},warn(){}},setInterval(fn){polls.push(fn);return polls.length;},
  clearInterval(){},setTimeout(){},clearTimeout(){}});
const flush=()=>{while(queued.length){const data=queued.shift();
  for(const fn of handlers)fn({source:window,data});}};
vm.runInContext(fs.readFileSync('extension/content.js','utf8'),pageContext);
vm.runInContext(fs.readFileSync('extension/nowplaying_main.js','utf8'),pageContext);
flush();
for(const fn of polls)fn();flush(); // Repeated observation of the same song cannot add a play.
current='next';
for(const fn of polls)fn();flush();
(async()=>{
  await worker.playback.drained;
  const playback=frames.filter(frame=>['play','now-heartbeat'].includes(frame.type));
  assert(playback.length>0);
  assert(playback.every(frame=>frame.playlist==='PL_current'));
  process.stdout.write(JSON.stringify(playback));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    frames = json.loads(result.stdout)
    iid = store.identities.upsert_identity("music", "bridge", "brand-A", True)
    if scenario == "ambiguous_account":
        store.identities.upsert_identity("other", "bridge", "brand-A", False)
    if scenario == "generated":
        store.playlists.set_playlist_group("PL_current", "Generated")
        store.playlists.mark_generated_playlist("PL_current")
    ctx = SimpleNamespace(store=store)
    plays = [frame for frame in frames if frame["type"] == "play"]
    for index, frame in enumerate(plays):
        handle_play_event(ctx, frame, 1000.0 + index * 300)

    events = store.history.play_events_since(0)
    if scenario in {"organic", "generated"}:
        assert [frame["videoId"] for frame in plays] == ["current", "next"]
        assert all(frame["metadataVerified"] is True and frame["brandId"] == "brand-A" for frame in plays)
        assert [event["video_id"] for event in events] == ["current", "next"]
        assert [event["played_at"] for event in events] == [1000.0, 1300.0]
        assert all(event["identity_id"] == iid and event["playlist_ytm_id"] == "PL_current" for event in events)
        assert store.settings.get_setting("last_plays_sync_at") == "1300.0"
        assert [row[0] for row in store.conn.execute("SELECT is_generated FROM play_events")] == \
            [int(scenario == "generated")] * 2
    else:
        assert events == []
        assert store.settings.get_setting("last_plays_sync_at") is None
        if scenario != "ambiguous_account":
            assert plays == []
        if scenario in {"wrong_title", "wrong_artist", "missing_artist", "stale_target",
                        "conflicting_targets", "conflicting_bar"}:
            assert all(frame["metadataVerified"] is False for frame in frames)
