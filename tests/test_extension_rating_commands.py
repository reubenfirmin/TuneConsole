"""Rate controls verify a fresh MAIN identity and the player-bar binding before clicking."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("scenario", ["like", "dislike", "advance", "stale_bar", "unknown", "wrong_control"])
def test_rate_command_matches_current_player(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the extension command harness")
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const scenario=process.argv[1],handlers=[],queue=[],clicks=[];
let runtime, playerId='A',barId='A';
const video={paused:false,muted:false,volume:1,addEventListener(){},removeEventListener(){}};
const window={ytcfg:{get:key=>key==='LOGGED_IN'?true:'brand'},addEventListener(t,fn){if(t==='message')handlers.push(fn);},
 removeEventListener(){},postMessage(msg){queue.push(msg);}};
const document={addEventListener(){},querySelector(s){
 if(s==='video')return video;
 if(s==='#movie_player')return {getVideoData:()=>({video_id:playerId,title:'Song',author:'Artist'}),getPlaylistId:()=>''};
 if(s==='ytmusic-player-bar')return {data:{videoId:barId},getAttribute:()=>''};
 if(s==='.title.ytmusic-player-bar')return {textContent:'Song'};
 if(s==='.byline.ytmusic-player-bar')return {textContent:'Artist'};
 if(s==='img.image.ytmusic-player-bar')return {src:`https://i.ytimg.com/vi/${barId}/hqdefault.jpg`};
 if(s==='ytmusic-player-bar ytmusic-like-button-renderer')return {
   data:{target:{videoId:scenario==='wrong_control'?'B':barId}},getAttribute:()=>'',
   querySelector:id=>({getAttribute:()=>'',click(){clicks.push(id);}})};
 return null;
}};
const context=vm.createContext({window,document,navigator:{mediaSession:{metadata:null}},URL,Date,Intl,
 location:{href:'https://music.youtube.com/'},console:{log(){},warn(){}},
 chrome:{runtime:{onMessage:{addListener(fn){runtime=fn;}},sendMessage(){}}},
 setInterval(){},clearInterval(){},setTimeout(){return 1;},clearTimeout(){}});
const flush=()=>{while(queue.length){const data=queue.shift();for(const fn of handlers)fn({source:window,data});}};
vm.runInContext(fs.readFileSync('extension/content.js','utf8'),context);
vm.runInContext(fs.readFileSync('extension/nowplaying_main.js','utf8'),context);flush();
// Change playback without a periodic sample: the rating command must actively resample MAIN.
if(scenario==='advance')playerId=barId='B';
if(scenario==='stale_bar')playerId='B';
if(scenario==='unknown')playerId='';
let reply;
const action=scenario==='dislike'?'dislike':'like';
assert.equal(runtime({type:'rate',action,videoId:'A'},{},r=>reply=r),true);
assert.equal(clicks.length,0);flush();
assert.equal(reply.ok,['like','dislike'].includes(scenario));
assert.equal(clicks.length,reply.ok?1:0);
if(reply.ok)assert.equal(clicks[0],`#button-shape-${action} button`);
"""
    result = subprocess.run([node, "-e", script, scenario], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
