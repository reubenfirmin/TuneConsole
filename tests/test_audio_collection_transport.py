"""Exercise app reconnects with Chrome's one-sided local disconnect behavior."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_collection_transport_rejects_legacy_times_out_and_recovers():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/yt_playlist/web/static/audio-vibe-collection.js', 'utf8');
// Complete this event-loop turn, including all chained promise reactions.
// No elapsed-time delay or assumed number of microtasks supplies completion.
const settle = async () => new Promise(resolve => setImmediate(resolve));
async function fixture(ids=['legacy']) {
  let now=0, nextTimer=1;
  const timers=new Map(), listeners={}, storage=new Map(), ports=[], cards=[];
  const root={dataset:{extensionIds:ids.join(','),taxonomyVersion:'current',choices:'{}'}};
  const window={playbackStatus:{snapshot:{connected:true,now_playing:{
    video_id:'first',title:'First song',position:1,duration:240,paused:false,snapshot_age:0}}},
    addEventListener:(name,fn)=>{listeners[name]=fn}};
  const context={window,navigator:{},location:{pathname:'/audio-vibes'},
    document:{querySelector:()=>root},fixtureCards:{
    accept:value=>cards.push(structuredClone(value)),tick(){}},
    localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value)},
    AbortController, console, crypto:require('node:crypto').webcrypto,
    Date:class extends Date {static now(){return now}},
    setTimeout:(fn,ms)=>{const id=nextTimer++;timers.set(id,{fn,at:now+ms});return id},
    clearTimeout:id=>timers.delete(id),setInterval:fn=>{listeners.tick=fn;return 1},clearInterval(){},
    fetch:async url=>({ok:true,json:async()=>({run_id:new URL(url,'http://fixture').pathname.split('/').at(-1),
      samples:[],taxonomy_version:'current'})}),
    chrome:{runtime:{connect(id){
      if(id==='throws') throw Error('extension missing');
      const messages=[],disconnections=[];
      const port={id,closed:false,sent:[],postMessage:data=>port.sent.push(data),
        onMessage:{addListener:fn=>messages.push(fn)},onDisconnect:{addListener:fn=>disconnections.push(fn)},
        disconnect(){port.closed=true}, // Chrome does NOT notify this end.
        message:data=>messages.forEach(fn=>fn(data)),remoteClose:()=>disconnections.forEach(fn=>fn())};
      ports.push(port);return port;
    }}}};
  const realm = vm.createContext({...context, URL});
  const module = new vm.SourceTextModule(source, {context: realm,
    initializeImportMeta: meta => {meta.url = 'http://fixture/static/audio-vibe-collection.js?v=owned-asset';},
    importModuleDynamically: async specifier => {
      assert.equal(specifier, './audio-vibe-banner.js?v=owned-asset');
      const banner = new vm.SyntheticModule(['createCards'], function () {
        this.setExport('createCards', async () => context.fixtureCards);
      }, {context: realm});
      await banner.link(() => {throw Error('Unexpected banner import');});
      await banner.evaluate();
      return banner;
    }});
  await module.link(() => {throw Error('Unexpected import');});
  await module.evaluate();
  await settle();
  return {context,root,ports,cards,timers,storage,listeners,window,
    async advance(ms){now+=ms;for(const [id,timer] of [...timers]) if(timer.at<=now){
      timers.delete(id);timer.fn();}await settle();},
    current:()=>cards.at(-1),run:()=>JSON.parse(storage.get('tuneconsole.audio-vibes.run.1'))};
}
(async()=>{
  const legacy=await fixture(['legacy','absent']);
  legacy.ports[0].message({type:'ready'});
  assert.equal(legacy.ports[0].closed,true);
  await settle();
  assert.equal(legacy.current().playback.title,'First song','incompatible handshake immediately admits human feedback');
  assert.equal(legacy.current().samples.length,0);
  const first=legacy.run().id;
  assert.equal(legacy.current().collection_status.update,true);
  assert.match(legacy.current().collection_status.message,/0\.1\.24/);
  legacy.ports[1].remoteClose();await settle();
  assert.equal(legacy.current().collection_status.update,true,'missing other IDs cannot erase upgrade diagnosis');
  assert.equal(legacy.timers.size,0,'all handshake/request timers settle');
  await legacy.advance(2000);legacy.listeners.tick();await settle();
  assert.equal(legacy.ports.length,4,'local disconnect cannot leave reconnect blocked');
  legacy.ports[2].message({type:'ready',protocol:1,extension_version:'0.1.24'});await settle();
  assert.equal(legacy.root.dataset.extensionVersion,'0.1.24');
  assert.equal(legacy.root.dataset.collectionProtocol,'1');
  assert.equal(legacy.current().collection_status,null,'successful upgrade clears the stale notice');
  assert.equal(legacy.ports[3].closed,true);
  assert.equal(legacy.timers.size,0,'winning candidate cancels competing timeouts');
  assert.equal(legacy.ports[2].sent[0].run.id,first,'restore preserves the zero-check human occurrence');
  legacy.ports[3].message({type:'ready'});await settle();
  assert.equal(legacy.current().collection_status,null,'late losing messages cannot replace a healthy owner');
  legacy.listeners.pagehide();await settle();
  assert.equal(legacy.ports[2].closed,true);
  assert.equal(legacy.timers.size,0);

  const silent=await fixture(['silent']);
  await silent.advance(3000);
  const silentFirst=silent.run().id;
  assert.equal(silent.ports[0].closed,true);
  assert.equal(silent.current().collection_status.phase,'unavailable');
  silent.listeners.tick();await settle();
  assert.equal(silent.ports.length,2,'a timed-out local close must also allow another attempt');
  silent.ports[0].message({type:'ready',protocol:1});await settle();
  assert.equal(silent.root.dataset.collectionProtocol,undefined,'late timed-out ready must be ignored');
  silent.ports[1].remoteClose();await settle();
  silent.window.playbackStatus.snapshot.now_playing={...silent.window.playbackStatus.snapshot.now_playing,
    video_id:'second',title:'Second song'};
  silent.listeners['bridge-status']();await settle();
  assert.equal(silent.current().playback.title,'Second song');
  assert.notEqual(silent.run().id,silentFirst);
  assert.equal(silent.current().samples.length,0);

  const throwing=await fixture(['throws']);
  assert.equal(throwing.current().collection_status.phase,'unavailable');
  assert.equal(throwing.current().playback.title,'First song');

  const healthy=await fixture(['current']);
  assert.equal(healthy.cards.length,0,'await the owner identity before inventing a parallel playback occurrence');
  healthy.ports[0].message({type:'ready',protocol:1,extension_version:'0.1.24'});
  healthy.ports[0].message({type:'collection',protocol:1,run:{id:'owned',video_id:'first',
    title:'First song',points:{}},recent:[]});await settle();
  assert.equal(healthy.run().id,'owned');
  assert(healthy.cards.every(card=>card.run_id==='owned'));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, '--experimental-vm-modules', '-e', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
