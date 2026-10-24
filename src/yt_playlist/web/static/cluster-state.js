// Detached graph transformations. No browser storage, drawing, requests or Alpine dependency.
const ClusterGraph = (() => {
  function branchId(node, byId, rootId) {
    let cur = node;
    while (cur && cur.parentId != null && cur.parentId !== rootId) cur = byId.get(cur.parentId);
    return cur ? cur.id : null;
  }
  function topology(nodes, rootId) {
    const byId = new Map(nodes.map(n => [n.id, n])), child = new Map(), desc = new Map(), branch = new Map();
    for (const n of nodes) {
      if (n.parentId == null) continue;
      if (!child.has(n.parentId)) child.set(n.parentId, []);
      child.get(n.parentId).push(n);
    }
    const count = id => {
      if (desc.has(id)) return desc.get(id);
      desc.set(id, 0);
      const children = child.get(id) || [];
      const total = children.length + children.reduce((sum, n) => sum + count(n.id), 0);
      desc.set(id, total); return total;
    };
    for (const n of nodes) {
      count(n.id);
      branch.set(n.id, n.kind === 'central' ? null : branchId(n, byId, rootId));
    }
    return { byId, child, desc, branch };
  }
  function descendants(nodes, id) {
    const out = new Set(), stack = [id];
    while (stack.length) {
      const p = stack.pop();
      nodes.filter(n => n.parentId === p).forEach(c => { out.add(c.id); stack.push(c.id); });
    }
    return out;
  }
  function addChildren({nodes, nextId, subHues, subHueN}, parent, ring, distance) {
    subHues = {...subHues};
    if (!(parent.id in subHues)) subHues[parent.id] = Math.round((subHueN++ * 137.508) % 360);
    const gp = parent.parentId != null ? nodes.find(n => n.id === parent.parentId) : null;
    const outward = gp ? Math.atan2(parent.y - gp.y, parent.x - gp.x) : 0;
    const n = ring.length, spread = gp ? Math.min(Math.PI, n * 0.5) : 2 * Math.PI;
    const added = ring.map((t, i) => {
      const frac = n > 1 ? i / (gp ? n - 1 : n) : 0.5, angle = outward + (frac - 0.5) * spread;
      return { id: nextId++, parentId: parent.id, kind: 'track', key: t.key,
        label: t.title, sub: t.artist, thumbnail: t.thumbnail, vid: t.video_id,
        genre: t.genre || '', family: t.family || '', newMusic: !!t.out_of_corpus,
        state: 'neutral', depth: parent.depth + 1,
        x: parent.x + Math.cos(angle) * distance, y: parent.y + Math.sin(angle) * distance };
    });
    return {nodes: [...nodes, ...added], nextId, subHues, subHueN};
  }
  function prune(nodes, trunk, id) {
    const node = nodes.find(n => n.id === id);
    if (!node || node.kind === 'central') return null;
    const restoring = node.state === 'pruned';
    const removed = restoring ? new Set() : descendants(nodes, id);
    return {removed, nodes: nodes.filter(n => !removed.has(n.id)).map(n => n.id === id
      ? {...n, state: restoring ? 'neutral' : 'pruned', gpruned: false} : n),
      trunk: restoring ? trunk : trunk.filter(t => t !== id && !removed.has(t))};
  }
  function genreMatches(node, allowed) {
    if (!allowed.length || node.kind !== 'track') return true;
    const tokens = allowed.map(t => t.toLowerCase());
    return tokens.includes((node.genre || '').toLowerCase()) || tokens.includes((node.family || '').toLowerCase());
  }
  function genrePrune(nodes, trunk, allowed) {
    return nodes.map(n => {
      if (n.kind !== 'track' || trunk.includes(n.id) || nodes.some(c => c.parentId === n.id)) return n;
      const match = genreMatches(n, allowed);
      if (!match && n.state === 'neutral') return {...n, state: 'pruned', gpruned: true};
      if (match && n.state === 'pruned' && n.gpruned) return {...n, state: 'neutral', gpruned: false};
      return n;
    });
  }
  // d3 owns the simulation nodes. The force uses only their detached branch assignments.
  function branchForce() {
    let nodes = [];
    function force(alpha) {
      const k = alpha * 0.9, sx = new Map(), sy = new Map(), cnt = new Map();
      for (const n of nodes) {
        const b = n._b; if (b == null) continue;
        sx.set(b, (sx.get(b) || 0) + n.x); sy.set(b, (sy.get(b) || 0) + n.y); cnt.set(b, (cnt.get(b) || 0) + 1);
      }
      const bs = [...cnt.keys()]; if (bs.length < 2) return;
      const cx = new Map(), cy = new Map(), rad = new Map();
      for (const b of bs) { const c = cnt.get(b); cx.set(b, sx.get(b) / c); cy.set(b, sy.get(b) / c); rad.set(b, 150 + Math.sqrt(c) * 115); }
      const dx = new Map(), dy = new Map();
      for (let i = 0; i < bs.length; i++) {
        const bi = bs[i], xi = cx.get(bi), yi = cy.get(bi);
        for (let j = i + 1; j < bs.length; j++) {
          const bj = bs[j];
          let ex = xi - cx.get(bj), ey = yi - cy.get(bj), d = Math.hypot(ex, ey) || 1;
          const want = rad.get(bi) + rad.get(bj);
          if (d < want) {
            if (d < 1) { ex = (i - j) || 1; ey = ((i + 1) * (j + 2)) % 3 - 1 || 1; d = Math.hypot(ex, ey); }
            const push = (want - d) / d * k * 0.5;
            dx.set(bi, (dx.get(bi) || 0) + ex * push); dy.set(bi, (dy.get(bi) || 0) + ey * push);
            dx.set(bj, (dx.get(bj) || 0) - ex * push); dy.set(bj, (dy.get(bj) || 0) - ey * push);
          }
        }
      }
      for (const n of nodes) { const b = n._b; if (b == null) continue; const px = dx.get(b); if (px) { n.vx += px; n.vy += dy.get(b); } }
    }
    force.initialize = n => { nodes = n; };
    return force;
  }
  return {topology, branchId, descendants, addChildren, prune, genreMatches, genrePrune, branchForce};
})();
