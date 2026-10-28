"""Rate controls verify a fresh MAIN identity and the player-bar binding before clicking."""
from pathlib import Path
import resource
import shutil
import subprocess
import time

import pytest


@pytest.mark.parametrize("scenario", [
    "like", "dislike", "advance", "stale_bar", "unknown", "wrong_control",
    "unknown_account", "unverified_credits", "album_art", "like_again", "dislike_again",
    "unlike", "clear_dislike", "switch_to_like", "switch_to_dislike",
    "dislike_skip", "dislike_skip_already_set", "dislike_auto_advance", "dislike_no_next",
    "dislike_wrong_account", "next", "next_stale", "next_disabled",
    "rating_target_only", "controller_data", "conflicting_controller", "missing_binding",
    "delayed_binding", "expired_before_probe", "expired_during_probe", "cancelled_probe", "superseded_probe",
    "unknown_expected_account",
    "unconfirmed_click", "receiver_upgrade", "receiver_legacy",
])
def test_rate_command_matches_current_player(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the extension command harness")
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const scenario=process.argv[1],handlers=[],queue=[],clicks=[],snapshots=[],timers=new Map(),events=[];
const checkpoint = phase => fs.writeSync(2, JSON.stringify({scenario, phase,
  uptime: process.uptime(), usage: process.resourceUsage()}) + '\n');
checkpoint('started');
const albumArt=['album_art','rating_target_only','controller_data','conflicting_controller','missing_binding','delayed_binding'].includes(scenario);
let runtime, runtimeCount=0, timerSerial=0, playerId='A',barId='A',bindingReady=false,clock=1000;
const missingBinding=()=>scenario==='missing_binding'||(scenario==='delayed_binding'&&!bindingReady);
const initialStatus={like_again:'LIKE',dislike_again:'DISLIKE',unlike:'LIKE',
 clear_dislike:'DISLIKE',switch_to_like:'DISLIKE',switch_to_dislike:'LIKE',
 dislike_skip_already_set:'DISLIKE'}[scenario] || 'INDIFFERENT';
let ratingStatus=initialStatus;
const video={paused:false,muted:false,volume:1,addEventListener(){},removeEventListener(){}};
const window={ytcfg:['unknown_account','unknown_expected_account'].includes(scenario)?null:{get:key=>key==='LOGGED_IN'?true:'brand'},addEventListener(t,fn){if(t==='message')handlers.push(fn);},
 removeEventListener(t,fn){if(t==='message')handlers.splice(handlers.indexOf(fn),1);},
 postMessage(msg){queue.push(msg);if(msg.__tcNow)snapshots.push(msg.__tcNow);}};
const document={addEventListener(){},querySelector(s){
 if(s==='video')return video;
 if(s==='#movie_player')return {getVideoData:()=>({video_id:playerId,title:'Song',
   author:scenario==='rating_target_only'?'Uploader channel':'Artist'}),getPlaylistId:()=>''};
 if(s==='ytmusic-player-bar')return {
   data:['rating_target_only','controller_data'].includes(scenario)||missingBinding()?undefined:{videoId:barId},
   polymerController:scenario==='controller_data'?{data:{videoId:barId}}:
     scenario==='conflicting_controller'?{data:{videoId:'B'}}:undefined,getAttribute:()=>''};
 if(s==='.title.ytmusic-player-bar')return {textContent:'Song'};
 if(s==='.byline.ytmusic-player-bar')return {textContent:scenario==='unverified_credits'?'':'Artist'};
 if(s==='img.image.ytmusic-player-bar')return {src:albumArt?
   'https://lh3.googleusercontent.com/album':`https://i.ytimg.com/vi/${barId}/hqdefault.jpg`};
 if(s==='ytmusic-player-bar .next-button')return scenario==='dislike_no_next'?null:{
   disabled:scenario==='next_disabled',getAttribute:()=>'',querySelector:()=>null,
   click(){clicks.push('next');playerId=barId='B';}};
 if(s==='ytmusic-player-bar ytmusic-like-button-renderer')return {
   data:scenario==='controller_data'||missingBinding()?undefined:
     {target:{videoId:scenario==='wrong_control'?'B':barId}},
   polymerController:scenario==='controller_data'?{data:{target:{videoId:barId}}}:undefined,
   getAttribute:()=>ratingStatus,
   querySelector:id=>({getAttribute:()=>'',click(){
     clicks.push(id);
     if(scenario==='unconfirmed_click')return;
     const want=id==='#button-shape-like button'?'LIKE':'DISLIKE';
     ratingStatus=ratingStatus===want?'INDIFFERENT':want;
     if(scenario==='dislike_auto_advance')playerId=barId='B';
   }})};
 return null;
}};
const context=vm.createContext({window,document,navigator:{mediaSession:{metadata:null}},URL,
 Date:class extends Date{static now(){return clock;}},Intl,
 location:{href:'https://music.youtube.com/'},console:{log(){},warn(){},debug(){}},
 chrome:{runtime:{onMessage:{addListener(fn){runtime=fn;runtimeCount++;},
  removeListener(fn){assert.equal(runtime,fn);runtime=null;runtimeCount--;}},sendMessage(msg){events.push(msg);}}},
 setInterval(){},clearInterval(){},setTimeout(fn){const id=++timerSerial;timers.set(id,fn);return id;},
 clearTimeout(id){timers.delete(id);}});
const flush=()=>{while(queue.length){const data=queue.shift();for(const fn of handlers)fn({source:window,data});}};
if(scenario==='receiver_legacy')window.__tcBridgeLoaded=true;
vm.runInContext(fs.readFileSync('extension/content.js','utf8'),context);
checkpoint('receiver-loaded');
if(scenario==='receiver_legacy'){
  assert.equal(runtimeCount,0,'cannot safely replace an anonymous legacy receiver');process.exit(0);
}
vm.runInContext(fs.readFileSync('extension/nowplaying_main.js','utf8'),context);flush();
checkpoint('sensor-flushed');
if(scenario==='receiver_upgrade'){
  const previous=window.__tcBridgeLoaded;
  previous.revision=0;
  vm.runInContext(fs.readFileSync('extension/content.js','utf8'),context);
  assert.equal(previous.stopped,true);
  assert.equal(runtimeCount,1,'replace the old command handler before installing another');
  assert.equal(handlers.length,2,'one MAIN and one isolated listener remain');
  vm.runInContext(fs.readFileSync('extension/content.js','utf8'),context);
  assert.equal(runtimeCount,1,'same-revision injection is idempotent');
}
// Change playback without a periodic sample: the rating command must actively resample MAIN.
if(['advance','next_stale'].includes(scenario))playerId=barId='B';
if(scenario==='stale_bar')playerId='B';
if(scenario==='unknown')playerId='';
let reply;
if(scenario==='expired_before_probe')clock=3000;
const action=scenario.startsWith('dislike') || scenario==='switch_to_dislike'?'dislike':'like';
const desiredStatus={like_again:'LIKE',dislike_again:'DISLIKE',unlike:'INDIFFERENT',
 clear_dislike:'INDIFFERENT',switch_to_like:'LIKE',switch_to_dislike:'DISLIKE'}[scenario] ||
 (scenario.startsWith('dislike_')?'DISLIKE':['delayed_binding','unconfirmed_click'].includes(scenario)?'LIKE':undefined);
const skip=scenario.startsWith('dislike_') && scenario!=='dislike_again';
assert.equal(runtime({type:scenario.startsWith('next')?'next':'rate',action,videoId:'A',
 likeStatus:desiredStatus,skip,ratingId:'one',expiresAt:2000,
 brandId:scenario==='dislike_wrong_account'?'other':scenario==='unknown_expected_account'?'brand':undefined},{},r=>reply=r),true);
if(scenario==='expired_during_probe')clock=3000;
if(scenario==='cancelled_probe')runtime({type:'cancel-rating',ratingId:'one'},{},()=>{});
if(scenario==='superseded_probe')runtime({type:'rate',ratingId:'two',action:'like',videoId:'A',
 likeStatus:'INDIFFERENT',brandId:'brand',expiresAt:2000},{},()=>{});
assert.equal(clicks.length,0);flush();
checkpoint('command-flushed');
if(scenario==='unconfirmed_click'){
  assert.equal(reply,undefined);
  assert.equal(clicks.length,1);
  clock=4000;
  for(const [id,fn] of [...timers]){timers.delete(id);fn();flush();}
  assert.equal(reply.ok,false);
  assert.equal(reply.retryable,undefined,'a click awaiting confirmation is never safe to replay');
  assert.equal(clicks.length,1);process.exit(0);
}
if(scenario==='delayed_binding'){
  assert.equal(reply.retryable,true);assert.equal(clicks.length,0);
  assert.equal(reply.diagnostics.receiverRevision,
    Number(fs.readFileSync('extension/content.js','utf8').match(/const RECEIVER_REVISION = (\d+)/)[1]));
  assert.equal(reply.diagnostics.mainRevision,
    Number(fs.readFileSync('extension/nowplaying_main.js','utf8').match(/var SENSOR_REVISION = (\d+)/)[1]));
  assert.equal(reply.diagnostics.binding.renderer,true);
  assert.equal(reply.diagnostics.binding.targetIds.length,0);
  bindingReady=true;
  for(let i=0;i<2;i++){
    runtime({type:'rate',action:'like',videoId:'A',likeStatus:'LIKE',brandId:'brand',expiresAt:2000},{},r=>reply=r);flush();
    assert.equal(reply.ok,true);
  }
  assert.equal(clicks.length,1,'one exact desired-state click after the binding settles');
  process.exit(0);
}
const rejected=['advance','stale_bar','unknown','wrong_control','next_stale','next_disabled',
 'dislike_no_next','dislike_wrong_account','conflicting_controller','missing_binding',
 'expired_before_probe','expired_during_probe','cancelled_probe','superseded_probe','unknown_expected_account'].includes(scenario);
const alreadySet=['like_again','dislike_again','dislike_skip_already_set'].includes(scenario);
assert.equal(reply.ok,!rejected);
if(['wrong_control','conflicting_controller','missing_binding'].includes(scenario))assert.equal(reply.retryable,true);
if(scenario==='rating_target_only'){
  const snapshot=snapshots.at(-1);
  assert.equal(snapshot.playerBar.ratingVideoId,'A');
  assert.equal(snapshot.playerBar.videoId,'');
  assert.equal(snapshot.metadataVerified,false,'rating identity cannot certify missing recording credits');
}
if(scenario==='dislike_skip')assert.deepEqual(clicks,['#button-shape-dislike button','next']);
else if(scenario==='dislike_skip_already_set' || scenario==='next')assert.deepEqual(clicks,['next']);
else if(scenario==='dislike_auto_advance' || scenario==='dislike_no_next')
 assert.deepEqual(clicks,['#button-shape-dislike button']);
else assert.equal(clicks.length,rejected || alreadySet?0:1);
if(scenario.startsWith('next')){
 assert.equal(ratingStatus,'INDIFFERENT');
 const requests=events.filter(m=>m.type==='pevent'&&m.kind==='next');
 assert.equal(requests.length,scenario==='next_stale'?0:1);
 if(requests.length){assert.equal(requests[0].videoId,'A');assert.equal(requests[0].brandKnown,true);}
 process.exit(0);
}
if(reply.ok){
  assert.equal(ratingStatus,desiredStatus || (action==='like'?'LIKE':'DISLIKE'));
  if(clicks.length && scenario!=='dislike_skip_already_set')
    assert.equal(clicks[0],`#button-shape-${scenario==='clear_dislike'?'dislike':action} button`);
}
"""
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired as error:
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
        diagnostics = error.stderr or "Node did not reach its first checkpoint."
        if isinstance(diagnostics, bytes):
            diagnostics = diagnostics.decode("utf-8", errors="replace")
        pytest.fail(f"Node rating scenario {scenario!r} timed out: "
                    f"wall={time.monotonic() - started:.3f}s, child CPU={cpu:.3f}s.\n"
                    f"{diagnostics}", pytrace=False)
    assert result.returncode == 0, result.stderr
