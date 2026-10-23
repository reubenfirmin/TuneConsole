// The v1 saved-canvas schema and browser storage layout have one owner.
const ClusterCanvases = (() => {
  const key = id => 'tc:canvas:' + id;
  const newId = () => 'c' + Date.now().toString(36) + Math.floor(Math.random() * 46656).toString(36);
  function label(state) {
    try {
      const root = (state.nodes || []).find(n => n.id === state.rootId);
      return (state.playlistName || root?.seeds?.[0]?.label || 'Untitled').slice(0, 28);
    } catch (_) { return 'Untitled'; }
  }
  function snapshot(graph, controls, view) {
    return {v: 1, nodes: graph.nodes, nextId: graph.nextId, rootId: graph.rootId, trunk: graph.trunk,
      subHues: graph.subHues, subHueN: graph.subHueN, allowedFamilies: controls.allowedFamilies,
      boosted: controls.boosted, playlistName: controls.playlistName, saveMode: controls.saveMode,
      journey: controls.journey, journeyName: controls.journeyName, randomSeed: controls.randomSeed, view};
  }
  function decode(state, randomSeed) {
    if (!state || state.v !== 1 || !Array.isArray(state.nodes) || !state.nodes.length) return null;
    return {...state, trunk: state.trunk || [], subHues: state.subHues || {}, subHueN: state.subHueN || 0,
      allowedFamilies: state.allowedFamilies || [], boosted: state.boosted || [],
      playlistName: state.playlistName || '', saveMode: state.saveMode || 'all',
      journey: state.journey || 'auto', journeyName: state.journeyName || 'Pick for me',
      randomSeed: state.randomSeed ?? randomSeed(), view: state.view || null};
  }
  function open(storage) {
    const get = name => { try { return storage.getItem(name); } catch (_) { return null; } };
    const put = (name, value) => { try { storage.setItem(name, value); } catch (_) {} };
    const drop = name => { try { storage.removeItem(name); } catch (_) {} };
    const list = () => { try { return JSON.parse(get('tc:canvases')) || []; } catch (_) { return []; } };
    const saveList = rows => put('tc:canvases', JSON.stringify(rows));
    const active = () => get('tc:active') || null;
    const setActive = id => id ? put('tc:active', id) : drop('tc:active');
    function read(id) { try { return JSON.parse(get(key(id))); } catch (_) { return null; } }
    function remove(id) {
      const rows = list().filter(c => c.id !== id);
      if (id) drop(key(id));
      saveList(rows); setActive(null); return rows;
    }
    function write(state, id) {
      if (!state.nodes.length) { if (id) remove(id); return null; }
      id = id || newId(); setActive(id);
      put(key(id), JSON.stringify(state));
      const rows = list(), title = label(state), i = rows.findIndex(c => c.id === id);
      if (i >= 0) rows[i].label = title; else rows.push({id, label: title});
      saveList(rows); return id;
    }
    function migrate() {
      if (get('tc:canvases')) { drop('tc:cluster'); return; }
      const old = get('tc:cluster');
      try {
        const state = old ? JSON.parse(old) : null;
        if (state && (state.nodes || []).length) {
          const id = newId(); put(key(id), old);
          saveList([{id, label: label(state)}]); setActive(id);
        }
        drop('tc:cluster');
      } catch (_) {}
    }
    return {list, active, setActive, read, remove, write, migrate};
  }
  return {key, newId, label, snapshot, decode, open};
})();
