"""Detached canvas graph and storage preserve editing and saved-state contracts."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_graph_transforms_and_saved_canvas_migration():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for canvas state checks")
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const context = vm.createContext({});
for (const name of ['cluster-state','cluster-storage']) {
  vm.runInContext(fs.readFileSync(`src/yt_playlist/web/static/${name}.js`,'utf8'),context);
}
vm.runInContext(`
const root = {id:1,parentId:null,kind:'central',depth:0,x:0,y:0,seeds:[{label:'Artist'}]},
  a = {id:2,parentId:1,kind:'track',state:'neutral',key:'a',depth:1,x:215,y:0},
  b = {id:3,parentId:2,kind:'track',state:'neutral',key:'b',depth:2,x:430,y:0},
  c = {id:4,parentId:1,kind:'track',state:'pruned',key:'c',gpruned:false};
const nodes = [root,a,b,c], before = JSON.stringify(nodes);
const topo = ClusterGraph.topology(nodes,1);
if (topo.desc.get(1)!==3 || topo.desc.get(2)!==1 || topo.branch.get(3)!==2) throw Error('topology');
const pruned = ClusterGraph.prune(nodes,[2,3],2);
if (pruned.nodes.length!==3 || pruned.nodes[1].state!=='pruned' || pruned.trunk.length) throw Error('prune descendants');
const restored = ClusterGraph.prune(pruned.nodes,pruned.trunk,2);
if (restored.nodes[1].state!=='neutral' || restored.nodes[1].gpruned) throw Error('restore');
const filtered = ClusterGraph.genrePrune(nodes,[2],['Techno']);
if (filtered[1].state!=='neutral' || !filtered[2].gpruned || filtered[3].gpruned) throw Error('genre policy');
const cleared = ClusterGraph.genrePrune(filtered,[2],[]);
if (cleared[2].state!=='neutral' || cleared[3].state!=='pruned') throw Error('manual prune survives genre reset');
const next = ClusterGraph.addChildren({nodes,nextId:5,subHues:{},subHueN:0},a,
  [{key:'new',title:'New',artist:'Artist',video_id:'upload',out_of_corpus:true}],215);
if (next.nextId!==6 || next.subHueN!==1 || next.nodes[4].parentId!==2 || !next.nodes[4].newMusic) throw Error('ring metadata');
if (Math.abs(Math.hypot(next.nodes[4].x-a.x,next.nodes[4].y-a.y)-215)>1e-8) throw Error('ring geometry');
if (JSON.stringify(nodes)!==before) throw Error('input graph mutated');
const data = new Map(), storage = {getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)};
const saved = ClusterCanvases.snapshot({nodes,nextId:5,rootId:1,trunk:[2],subHues:{2:138},subHueN:2},
  {allowedFamilies:['Techno'],boosted:['a'],playlistName:'Night mix',saveMode:'trunk',journey:'auto',journeyName:'Pick for me',randomSeed:0},
  {z:0.7,cx:20,cy:30});
const raw = JSON.stringify(saved); data.set('tc:cluster',raw);
const canvases = ClusterCanvases.open(storage); canvases.migrate();
const id = canvases.active();
if (!id || data.has('tc:cluster') || data.get(ClusterCanvases.key(id))!==raw || canvases.list()[0].label!=='Night mix') throw Error('legacy migration');
const decoded = ClusterCanvases.decode(canvases.read(id),()=>{throw Error('random seed regenerated');});
if (decoded.randomSeed!==0 || decoded.view.z!==0.7 || decoded.boosted[0]!=='a') throw Error('restore controls');
canvases.write({...saved,playlistName:'Renamed'},id);
if (canvases.list().length!==1 || canvases.list()[0].label!=='Renamed') throw Error('rename duplicated canvas');
const other = canvases.write(saved,null); canvases.remove(id);
if (canvases.list().length!==1 || !canvases.read(other) || canvases.read(id) || canvases.active()) throw Error('remove scope');
const unavailable = ClusterCanvases.open({getItem(){throw Error('disabled');},setItem(){throw Error('disabled');},removeItem(){throw Error('disabled');}});
unavailable.migrate(); unavailable.write(saved,null);
if (unavailable.read(id)!==null || unavailable.list().length) throw Error('unavailable storage');
if (ClusterCanvases.decode({v:2,nodes},()=>0)!==null) throw Error('unsupported version');
`,context);
"""
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_search_survives_first_autosave_but_rejects_canvas_replacement():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for canvas request checks")
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const data = new Map(), pending = [];
const context = vm.createContext({
  pending, console, process,
  localStorage: {getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)},
  fetch: () => new Promise(resolve => pending.push(resolve)),
});
for (const name of ['cluster-state','cluster-storage','cluster-renderer','cluster'])
  vm.runInContext(fs.readFileSync(`src/yt_playlist/web/static/${name}.js`,'utf8'),context);
vm.runInContext(`
(async () => {
  const c = clusterCanvas();
  c.nodes = [{id:1,kind:'central',parentId:null,seeds:[{kind:'artist',label:'First',keys:['first']}]}];
  c.rootId = 1;
  c._feed = () => {};
  c.$nextTick = () => {};
  c.query = 'Second';
  const before = c._graphContext();
  const search = c.search();
  if (c.activeId !== null) throw Error('fresh canvas already persisted');
  c._writeState();
  if (!c.activeId || !c._currentGraph(before)) throw Error('autosave changed the logical canvas');
  const found = [{kind:'artist',label:'Second',keys:['second']}];
  pending.shift()({ok:true,json:async()=>found});
  const results = await search;
  if (results !== found || c.results !== found) throw Error('first autosave discarded valid search');

  const late = c.search(), previous = c._graphContext();
  // Identical graph data in a replacement still belongs to a different request generation.
  c._applyState(JSON.parse(c.clusterStateJSON()));
  if (c._currentGraph(previous)) throw Error('replacement retained stale request context');
  const replacement = [{kind:'artist',label:'Replacement',keys:['replacement']}];
  c.results = replacement;
  pending.shift()({ok:true,json:async()=>found});
  await late;
  if (c.results !== replacement) throw Error('late response overwrote replacement canvas');
})().catch(error => { console.error(error); process.exitCode = 1; });
`, context);
"""
    result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
