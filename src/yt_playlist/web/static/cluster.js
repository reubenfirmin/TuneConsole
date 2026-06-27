// Clusters tab canvas: the Alpine component behind templates/clusters.html (x-data="clusterCanvas()").
// A pannable/zoomable d3-force graph you seed and grow into a playlist. Extracted from app.js to keep
// that file focused; loaded as a classic global script (see base.html) so clusterCanvas() is a global,
// exactly as before. Depends on the d3-force vendor bundle, which base.html loads ahead of this file.

// Clusters tab: a pannable/zoomable canvas where you seed a central group and grow a tree outward.
// Each node's next ring = library tracks nearest its pinned-path centroid, pushed away from pruned
// tracks (server: POST /clusters/expand). Tree state lives here; the server stays stateless. Node
// positions are owned by a live d3-force simulation (link + charge + collide) so the graph lays
// itself out without overlap and re-settles as you grow/prune. D3 mutates each node's x/y in place,
// which Alpine renders reactively.
// Keep a collection OUT of Alpine's reactivity. Alpine (Vue reactivity) re-proxies ANY object reached
// through `this`, so without this the d3 simulation and the per-frame renderer get handed proxied nodes
// again — a get/set trap on every node.x/vx read+write, every tick, plus reactive triggers on writes.
// Vue's '__v_skip' flag makes reactive() return the object untouched, so `this._raw`/`this._rawById`/the
// el-maps stay plain and the hot path touches raw objects only. (Stable across Alpine 3 / @vue/reactivity.)
function rawColl(o) { try { Object.defineProperty(o, '__v_skip', { value: true }); } catch (e) {} return o; }

function clusterCanvas() {
  const WORLD = 8000, CENTER = WORLD / 2;   // big fixed world; we pan/zoom a transform over it
  const LINK_D = 215, NODE_R = 128;         // spoke length; collision radius (#14: roomier layout)
  return {
    WORLD,
    nodes: [], nextId: 1, rootId: null,
    query: '', results: [], seedSel: -1, genreSel: -1,   // dropdown keyboard cursors
    playlistName: '', saveMode: 'all',   // 'all' non-hidden | 'trunk' only
    journey: 'auto', journeyName: 'Pick for me', journeyOpen: false,   // DJ-journey ordering pick
    tx: 0, ty: 0, scale: 1,
    _pan: null, _drag: null, sim: null,
    _byId: null, _topo: null,   // O(1) id index + per-reheat topology (branch/child/descendant counts)
    explain: null,            // {childId, loading, data}: the "why this edge?" popover
    families: [], genres: [], allowedFamilies: [], genreOpen: false, genreQuery: '',   // #29 genre whitelist (families + sub-genres)
    trunk: [],                // #30 ids of grown nodes; the edge leading into each lights as trunk
    subHues: {}, _subHueN: 0,  // #14 parentId -> hue: every grown ring (sub-cluster) gets its own colour
    exhaustedIds: [],          // nodes whose + found nothing left under the active genre filter
    boosted: [],               // 🔥 track keys to emphasize. Every future grow leans toward them

    init() {
      // The central group is pinned at CENTER (fx/fy), so it anchors the whole graph: no centering
      // force needed and the view never drifts off it. Everything else is positioned by charge +
      // collide (no overlap) + link (spokes), so cards radiate around the centre.
      const d3 = window.d3;
      this.sim = d3.forceSimulation([])
        .force('charge', d3.forceManyBody().strength(-420).distanceMax(1600))
        // One iteration mid-motion keeps the tick cheap; cards may overlap transiently while settling.
        // A synchronous collide-only pass on 'end' (see _settleOverlaps) guarantees the FINAL layout is
        // overlap-free, so the fast cool below never leaves cards stacked.
        .force('collide', d3.forceCollide(NODE_R).strength(0.95).iterations(1))
        .force('link', d3.forceLink([]).id(n => n.id).strength(0.5)
          // Spoke length grows with crowding: a busy ring needs longer spokes to fit around its
          // parent, and a child that has grown its OWN sub-cluster gets pushed further out so that
          // sub-cluster has clear room (recomputed on every _reheat).
          .distance(l => LINK_D
            + Math.min(220, Math.max(0, this.childCount(l.source.id) - 4) * 28)
            + Math.min(320, this.descCount(l.target.id) * 26)))
        .force('separate', this._clusterForce())   // keep distinct branches from overlapping
        .alphaDecay(0.1);                     // ~1s of animated settle (was ~3s); cools fully to the default alphaMin
      this.sim.stop();                       // started on demand once there are nodes (see _reheat)
      // On settle: resolve any residual overlap (the fast cool + 1-iteration collide can leave some),
      // then one crisp final frame, then save the settled positions.
      this.sim.on('end', () => { this._settleOverlaps(); this._renderFrame(true); this.persist(); });
      // The sim mutates node x/y in place on every tick. We do NOT let Alpine react to that (see
      // _renderFrame): positions are written to the DOM imperatively, once per animation frame, so the
      // physics never drives ~80 reactive style effects + proxy traps per tick. rAF-throttled.
      this.sim.on('tick.render', () => this._scheduleFrame());
      window.addEventListener('pagehide', () => this._flushState());   // land the latest state on navigation
      // #48: ?from=<ytm> reopens the canvas behind a saved cluster playlist (server-stored), overriding
      // the localStorage canvas. Strip the param so a later refresh keeps your edits instead of reloading.
      const from = new URLSearchParams(location.search).get('from');
      if (from) {
        history.replaceState(null, '', location.pathname);
        fetch('/clusters/state/' + encodeURIComponent(from))
          .then(r => r.ok ? r.json() : null)
          .then(s => this._afterInit(s && this._applyState(s)))
          .catch(() => this._afterInit(false));
      } else {
        this._afterInit(this.restore());     // bring back a canvas from a previous visit (localStorage)
      }
      fetch('/clusters/genres').then(r => r.json())
        .then(d => { this.families = d.families || []; this.genres = d.genres || []; }).catch(() => {});
    },
    _afterInit(restored) {
      this.$nextTick(() => {
        this._initGrid();
        this._structDirty = true; this._renderFrame(false);   // position the restored/initial cards before paint
        if (restored) { this.drawGrid(); return; }   // keep the saved view; nothing to focus into
        this.resetView();
        this.$refs.seedInput && this.$refs.seedInput.focus();
      });
    },

    // --- persistence: the whole canvas survives a refresh (localStorage) ---
    // Debounced: one interaction fires persist() many times (reheat, settle, drag/zoom end): coalesce
    // the bursts into a single write a beat after you stop. _flushState (pagehide) lands the latest
    // state even if you navigate away mid-debounce.
    persist() {
      clearTimeout(this._persistT);
      this._persistT = setTimeout(() => { this._persistT = 0; this._writeState(); }, 300);
    },
    // The serializable canvas: shared by localStorage persistence AND the save form, so reopening a
    // saved playlist (#48) restores the exact same graph that produced it.
    _stateBlob() {
      return {
        v: 1, nodes: this.nodes, nextId: this.nextId, rootId: this.rootId, trunk: this.trunk,
        subHues: this.subHues, subHueN: this._subHueN, allowedFamilies: this.allowedFamilies,
        boosted: this.boosted,
        playlistName: this.playlistName, saveMode: this.saveMode,
        journey: this.journey, journeyName: this.journeyName, tx: this.tx, ty: this.ty, scale: this.scale,
      };
    },
    // Snapshot the canvas for the save POST (#48). Set imperatively at submit time (not a reactive
    // :value binding) so stringifying every node doesn't run on every simulation tick.
    clusterStateJSON() { return this.nodes.length ? JSON.stringify(this._stateBlob()) : ''; },
    _writeState() {
      if (!this.nodes.length) { try { localStorage.removeItem('tc:cluster'); } catch (e) {} return; }  // nothing to keep
      try {
        localStorage.setItem('tc:cluster', JSON.stringify(this._stateBlob()));
      } catch (e) { /* private mode / quota: just don't persist */ }
    },
    _flushState() { if (this._persistT) { clearTimeout(this._persistT); this._persistT = 0; this._writeState(); } },
    // Load a serialized canvas (from localStorage OR the server, #48) into the live component. Feeds
    // the sim WITHOUT re-energizing, so positions are restored exactly. Returns true iff it applied.
    _applyState(s) {
      if (!s || s.v !== 1 || !Array.isArray(s.nodes) || !s.nodes.length) return false;
      this.nodes = s.nodes; this.nextId = s.nextId; this.rootId = s.rootId; this.trunk = s.trunk || [];
      this.subHues = s.subHues || {}; this._subHueN = s.subHueN || 0;
      this.allowedFamilies = s.allowedFamilies || []; this.boosted = s.boosted || [];
      this.playlistName = s.playlistName || '';
      this.saveMode = s.saveMode || 'all';
      this.journey = s.journey || 'auto'; this.journeyName = s.journeyName || 'Pick for me';
      this.tx = s.tx || 0; this.ty = s.ty || 0; this.scale = s.scale || 1;
      this._syncSim();
      return true;
    },
    restore() {
      let s;
      try { s = JSON.parse(localStorage.getItem('tc:cluster')); } catch (e) { return false; }
      return this._applyState(s);
    },
    _clearState() { clearTimeout(this._persistT); this._persistT = 0; try { localStorage.removeItem('tc:cluster'); } catch (e) {} },
    // Wipe the canvas back to a blank slate (explicit Reset button, and after a Save).
    reset() {
      this.nodes = []; this.nextId = 1; this.rootId = null; this.trunk = [];
      this.subHues = {}; this._subHueN = 0; this.exhaustedIds = []; this.boosted = [];
      this.allowedFamilies = []; this.genreQuery = ''; this.genreOpen = false;
      this.query = ''; this.results = []; this.playlistName = ''; this.saveMode = 'all';
      this.journey = 'auto'; this.journeyName = 'Pick for me'; this.explain = null;
      this._clearState();
      this._syncSim();
      this.$nextTick(() => { this.resetView(); this.$refs.seedInput && this.$refs.seedInput.focus(); });
    },
    // Feed the current nodes/links to the sim and re-settle. `alpha` controls how much the layout is
    // allowed to move: ~0.9 for a real grow (spread the new ring), but a small value for a removal so
    // the graph barely shifts (#14: pruning shouldn't jiggle everything).
    _reheat(alpha = 0.9) {
      this._recomputeTopology();
      const links = this._raw
        .filter(n => n.parentId != null && this._rawById.has(n.parentId))
        .map(n => ({ source: n.parentId, target: n.id }));
      this.sim.nodes(this._raw);
      this.sim.force('link').links(links);
      this._structDirty = true;              // a card may have been added/removed: rebuild the id→el map
      this.sim.alpha(alpha).restart();
      this.$nextTick(() => this._renderFrame(false));   // place the new ring before paint (don't wait for tick 1)
      this.persist();
    },
    // Update the sim's node/link sets WITHOUT re-energizing it. Removed nodes leave the simulation
    // but everything else holds its exact position (#14: pruning must not jiggle the layout).
    _syncSim() {
      this._recomputeTopology();
      const links = this._raw
        .filter(n => n.parentId != null && this._rawById.has(n.parentId))
        .map(n => ({ source: n.parentId, target: n.id }));
      this.sim.nodes(this._raw);
      this.sim.force('link').links(links);
      this._structDirty = true;
      this.$nextTick(() => this._renderFrame(false));   // positions/edges changed without re-energizing: redraw once
      this.persist();
    },

    // --- warped "spacetime" grid (a <canvas> behind the graph) ----------------------------------
    _initGrid() {
      if (this._gridCtx) return;             // idempotent: only wire up the canvas + resize listener once
      this._gridEl = this.$refs.grid; if (!this._gridEl) return;
      this._gridCtx = this._gridEl.getContext('2d');
      this._resizeGrid();
      window.addEventListener('resize', () => { this._resizeGrid(); this.drawGrid(); });
      this.drawGrid();
    },
    _resizeGrid() {
      const el = document.getElementById('cluster-canvas');
      if (!el || !this._gridEl) return;
      const r = el.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      this._gridEl.width = Math.max(1, Math.round(r.width * dpr));
      this._gridEl.height = Math.max(1, Math.round(r.height * dpr));
      this._gridEl.style.width = r.width + 'px';        // pin CSS size so backing-store ≠ display (hi-DPI)
      this._gridEl.style.height = r.height + 'px';
      this._gW = r.width; this._gH = r.height; this._gDpr = dpr;
    },
    // The warped grid is the heaviest paint on the canvas (O(samples × wells)) and barely changes
    // frame-to-frame while the layout settles. Cap it to ~16fps WHILE the sim is hot so the force ticks
    // and the imperative card writes keep the frame budget; pan/zoom (sim cold) and sim 'end' redraw
    // immediately. A throttled-out frame re-schedules on the next tick, so the grid keeps catching up.
    _drawGridIfDue(force) {
      const hot = this.sim && this.sim.alpha() > this.sim.alphaMin();
      const t = (window.performance && performance.now()) || 0;
      if (!force && hot && t - (this._lastGrid || 0) < 140) return;   // ~7fps while settling; the wells barely move frame-to-frame and 'end' draws crisp
      this._lastGrid = t;
      this.drawGrid();
    },
    // Cache the gravity-well glow as a sprite keyed by radius, so the grid blits it with drawImage
    // (GPU) instead of allocating a radial gradient + arc-fill for every well, every draw. Radius only
    // changes with zoom, so during a settle the same sprite is reused for all wells across all frames.
    _wellSprite(rad) {
      const key = Math.max(8, Math.round(rad));
      if (this._wellSpr && this._wellSprR === key) return this._wellSpr;
      const c = document.createElement('canvas');
      c.width = c.height = key * 2;
      const g2 = c.getContext('2d');
      const g = g2.createRadialGradient(key, key, key * 0.06, key, key, key);
      g.addColorStop(0, 'rgba(66,70,150,0.20)'); g.addColorStop(1, 'rgba(66,70,150,0)');
      g2.fillStyle = g; g2.beginPath(); g2.arc(key, key, key, 0, 7); g2.fill();
      this._wellSpr = c; this._wellSprR = key;
      return c;
    },
    // Grid-only frame: used by pan/zoom, where node world-positions don't change (the world transform
    // moves them) but the screen-space grid must follow the pan. Coalesced to one redraw per frame.
    _scheduleGrid() {
      if (this._gridRAF) return;
      this._gridRAF = requestAnimationFrame(() => { this._gridRAF = 0; this._drawGridIfDue(false); });
    },
    // Full frame: write every node's position to the DOM imperatively (cards, edge dots, the two edge
    // <path>s, the explain popover), then refresh the grid. Driven by the sim tick and by structural
    // changes. This is the ONLY place node x/y reach the DOM, which is why the sim can run on raw
    // objects without any Alpine reactivity in the hot path.
    _scheduleFrame() {
      if (this._frameRAF) return;
      this._frameRAF = requestAnimationFrame(() => {
        this._frameRAF = 0;
        // Mid-drag only ONE card moved (and it's already placed synchronously in onPanMove), so don't
        // rewrite all ~N card transforms — just refresh the edges and the dots touching the dragged node.
        if (this._drag && this._drag.moved) this._renderDragFrame();
        else this._renderFrame(false);
      });
    },
    // Cheap per-frame update while dragging: rebuild the two edge paths (the dragged node's spokes moved)
    // and reposition only the dots incident to the dragged node. No card transforms, no grid, no physics.
    _renderDragFrame() {
      const byId = this._rawById, dots = this._dotEls, dragId = this._drag.id, ts = new Set(this.trunk);
      let edge = '', trunk = '';
      for (const n of (this._raw || [])) {
        if (n.parentId == null) continue;
        const p = byId && byId.get(n.parentId); if (!p) continue;
        const seg = `M${p.x} ${p.y}L${n.x} ${n.y}`;
        if (ts.has(n.id)) trunk += seg; else edge += seg;
        if (dots && (n.id === dragId || n.parentId === dragId)) {   // only spokes touching the dragged card move
          const d = dots.get(n.id);
          if (d) d.style.transform = `translate(${(p.x + n.x) / 2}px,${(p.y + n.y) / 2}px)`;
        }
      }
      if (this.$refs.edgePath) this.$refs.edgePath.setAttribute('d', edge);
      if (this.$refs.trunkPath) this.$refs.trunkPath.setAttribute('d', trunk);
    },
    _rebuildElMap() {
      const world = this.$refs.world;
      this._cardEls = rawColl(new Map()); this._dotEls = rawColl(new Map());
      if (!world) return;
      world.querySelectorAll('.cluster-node[data-nid]').forEach(el => this._cardEls.set(+el.dataset.nid, el));
      world.querySelectorAll('.edge-dot[data-eid]').forEach(el => this._dotEls.set(+el.dataset.eid, el));
    },
    _renderFrame(force) {
      if (this._structDirty || !this._cardEls) { this._rebuildElMap(); this._structDirty = false; }
      const byId = this._rawById, cards = this._cardEls, dots = this._dotEls;
      for (const n of (this._raw || [])) {
        const c = cards.get(n.id);
        if (c) c.style.transform = `translate(${n.x}px,${n.y}px) translate(-50%,-50%)`;
        if (n.parentId == null) continue;
        const p = byId && byId.get(n.parentId); if (!p) continue;
        const d = dots.get(n.id);
        if (d) d.style.transform = `translate(${(p.x + n.x) / 2}px,${(p.y + n.y) / 2}px)`;
      }
      if (this.$refs.edgePath) {
        const e = this._edgePaths();
        this.$refs.edgePath.setAttribute('d', e.edge);
        if (this.$refs.trunkPath) this.$refs.trunkPath.setAttribute('d', e.trunk);
      }
      if (this.explain && this.$refs.explainEl) {
        const a = this.explainAnchor();
        this.$refs.explainEl.style.left = a.x + 'px';
        this.$refs.explainEl.style.top = a.y + 'px';
      }
      // The warped grid is the heaviest redraw, so freeze it while a node is being dragged (the cards
      // still flow); it recomputes on drop (onPanEnd) and on settle. `force` (settle / restore) always draws.
      if (force || !(this._drag && this._drag.moved)) this._drawGridIfDue(force);
    },
    // Resolve any overlap WITHOUT re-flowing the graph. forceCollide separates cards geometrically and
    // ignores alpha, so we zero alpha first: the other forces (charge/link/separate) scale to nothing and
    // only collide acts. A few synchronous ticks at higher iterations then nudge overlapping neighbours
    // apart in place — no link force pulling the layout around, so a drop never makes the graph "crawl".
    // tick() dispatches no events (no re-entrant 'end'/'tick'); the caller renders the one resulting frame.
    _settleOverlaps() {
      const collide = this.sim && this.sim.force('collide');
      if (!collide || !(this._raw && this._raw.length)) return;
      this.sim.alpha(0);                     // freeze flow: collide-only de-overlap, in place
      collide.iterations(3);
      for (let i = 0; i < 4; i++) this.sim.tick();
      collide.iterations(1);
    },
    // Draw a grid that warps toward each cluster centre (gravity-well "spacetime curvature"). Each
    // cluster's centre (the central group and every grown node) is a well whose depth grows with the
    // size of the cluster hanging off it; grid lines are pulled in (and a soft glow sinks the well) for
    // a 3-D dented-sheet read. The grid is world-attached (pans/zooms with the graph).
    drawGrid() {
      const ctx = this._gridCtx; if (!ctx) return;
      try {
      const W = this._gW, H = this._gH;
      ctx.setTransform(this._gDpr, 0, 0, this._gDpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      const scale = this.scale, tx = this.tx, ty = this.ty;
      const proj = (px, py) => [px * scale + tx, py * scale + ty];   // world → screen (glow wells only; the grid lines inline this)
      let wells = [];
      for (const n of (this._raw || [])) {
        if (n.kind === 'central' || this.childCount(n.id)) {
          wells.push({ x: n.x, y: n.y, m: (n.kind === 'central' ? 2.4 : 0.7) + this.descCount(n.id) * 0.3 });
        }
      }
      // cap the well count on huge clusters: the warp sums over every well per grid sample (O(pts·wells));
      // the heaviest few dominate the look anyway, so the rest add cost without visible benefit.
      if (wells.length > 16) wells = wells.sort((a, b) => b.m - a.m).slice(0, 16);
      const GRID = 88, R0 = 320, R0SQ = R0 * R0, PULL = 60;   // larger R0 ⇒ the well's pull reaches much further out
      // Flatten the wells into typed arrays once, so the inner sample loop (thousands of evals per draw)
      // reads contiguous numbers and never touches an object or allocates. f = mag / d, peaking AT the
      // well and decaying with distance, so the dent is deepest UNDER each centre.
      const wn = wells.length, wx = new Float64Array(wn), wy = new Float64Array(wn), wm = new Float64Array(wn);
      for (let i = 0; i < wn; i++) { wx[i] = wells[i].x; wy[i] = wells[i].y; wm[i] = PULL * wells[i].m * R0SQ; }
      const mg = GRID * 2;                             // visible world bounds (+ margin)
      const X0 = Math.floor(((0 - tx) / scale - mg) / GRID) * GRID, X1 = Math.ceil(((W - tx) / scale + mg) / GRID) * GRID;
      const Y0 = Math.floor(((0 - ty) / scale - mg) / GRID) * GRID, Y1 = Math.ceil(((H - ty) / scale + mg) / GRID) * GRID;
      const STEP = GRID / 5;                           // samples per grid cell: coarser curves, ~30% fewer warp evals
      // depth: a soft blue-gray glow that sinks each well (the bottom of the dent), centred on the node.
      // Blit a cached sprite per well (radius is constant across wells), not a fresh gradient each time.
      const rad = Math.max(40, R0 * scale * 0.85), spr = this._wellSprite(rad), d2r = rad * 2;
      for (const w of wells) {
        const [sx, sy] = proj(w.x, w.y);
        ctx.drawImage(spr, sx - rad, sy - rad, d2r, d2r);
      }
      ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(74,78,158,0.16)';   // dark blue-indigo, distinct from the neutral-gray edges
      for (let gx = X0; gx <= X1; gx += GRID) {        // vertical lines (constant world x)
        ctx.beginPath();
        for (let py = Y0, first = true; py <= Y1; py += STEP) {
          let dx = 0, dy = 0;                          // warp (gx, py) toward the wells, inlined (no alloc)
          for (let i = 0; i < wn; i++) {
            const ex = wx[i] - gx, ey = wy[i] - py, dd = ex * ex + ey * ey, d = Math.sqrt(dd) || 1;
            const f = wm[i] / (dd + R0SQ) / d; dx += ex * f; dy += ey * f;
          }
          const sx = (gx + dx) * scale + tx, sy = (py + dy) * scale + ty;
          if (first) { ctx.moveTo(sx, sy); first = false; } else ctx.lineTo(sx, sy);
        }
        ctx.stroke();
      }
      for (let gy = Y0; gy <= Y1; gy += GRID) {        // horizontal lines (constant world y)
        ctx.beginPath();
        for (let px = X0, first = true; px <= X1; px += STEP) {
          let dx = 0, dy = 0;                          // warp (px, gy)
          for (let i = 0; i < wn; i++) {
            const ex = wx[i] - px, ey = wy[i] - gy, dd = ex * ex + ey * ey, d = Math.sqrt(dd) || 1;
            const f = wm[i] / (dd + R0SQ) / d; dx += ex * f; dy += ey * f;
          }
          const sx = (px + dx) * scale + tx, sy = (gy + dy) * scale + ty;
          if (first) { ctx.moveTo(sx, sy); first = false; } else ctx.lineTo(sx, sy);
        }
        ctx.stroke();
      }
      } catch (e) { /* never let a draw error break panning/interaction */ }
    },
    // A d3 force that shoves cards of DIFFERENT branches apart when they crowd, so each sub-cluster
    // claims its own region instead of interleaving with a neighbour. Same-branch cards (and the
    // pinned centre) are left to the normal charge/collide/link forces.
    _clusterForce() {
      const self = this;
      let nodes = [];
      const SEP = 300, SEP2 = SEP * SEP;     // target clearance between cards across branches
      function force(alpha) {
        if (!self._topo) return;               // no topology yet (before the first reheat)
        const k = alpha * 0.6;
        // A quadtree prunes the cross-branch repulsion to nearby pairs only: ~O(n log n) instead of
        // the old all-pairs O(n²), which is what stalled the layout past ~80 cards. Each node is pushed
        // off every OTHER-branch neighbour within SEP; the symmetric push lands when that neighbour is
        // itself visited as `a`, so we only mutate `a` here (no double application). Branch ids are read
        // off the node (n._b, stamped in _recomputeTopology), so the inner loop does no Map lookups.
        const tree = window.d3.quadtree(nodes, n => n.x, n => n.y);
        for (let i = 0; i < nodes.length; i++) {
          const a = nodes[i];
          const ba = a._b;
          if (ba == null) continue;            // the pinned centre neither pushes nor is pushed
          const ax = a.x, ay = a.y;
          tree.visit((quad, x0, y0, x1, y1) => {
            if (x0 > ax + SEP || x1 < ax - SEP || y0 > ay + SEP || y1 < ay - SEP) return true;  // cell too far: prune
            if (quad.length) return false;     // internal node: descend
            let leaf = quad;
            do {
              const b = leaf.data;
              if (b !== a) {
                const bb = b._b;
                if (bb != null && bb !== ba) {   // skip same branch / centre
                  let dx = ax - b.x, dy = ay - b.y, d2 = dx * dx + dy * dy;
                  if (d2 === 0) { dx = 1; dy = (i % 7) - 3 || 1; d2 = dx * dx + dy * dy; }
                  if (d2 < SEP2) {
                    const d = Math.sqrt(d2), push = (SEP - d) / d * k;
                    a.vx += dx * push; a.vy += dy * push;
                  }
                }
              }
              leaf = leaf.next;
            } while (leaf);
            return false;
          });
        }
      }
      force.initialize = (n) => { nodes = n; };
      return force;
    },

    // --- search / seeding ---
    async search() {
      const q = this.query.trim();
      if (!q) { this.results = []; this.seedSel = -1; return; }
      try {
        const r = await fetch('/clusters/search?q=' + encodeURIComponent(q));
        this.results = await r.json();
      } catch (e) { this.results = []; }
      this.seedSel = -1;                       // reset the keyboard cursor on fresh results
    },
    // Arrow-key navigation for the seed-search dropdown.
    seedMove(d) {
      if (!this.results.length) return;
      this.seedSel = (this.seedSel + d + this.results.length) % this.results.length;
    },
    seedChoose() {
      const r = this.results[this.seedSel >= 0 ? this.seedSel : 0];
      if (r) this.addSeed(r);
    },
    // Every selection lands in ONE central group (artist + artist + song + …), pinned at the centre.
    // Its centroid is the union of all its seeds' keys. The FIRST seed grows the opening ring; each
    // ADDED seed refines the core's taste direction (#12). It re-ranks the whole tree in place
    // rather than bolting on another ring of cards.
    async addSeed(r) {
      this.query = ''; this.results = [];
      let root = this.rootId != null ? this.nodeById(this.rootId) : null;
      const fresh = !root;
      if (!root) {
        root = { id: this.nextId++, parentId: null, kind: 'central', state: 'central', depth: 0,
                 seeds: [], keys: [], key: null, vid: null,
                 x: CENTER, y: CENTER, fx: CENTER, fy: CENTER };
        this.rootId = root.id;
        this.nodes.push(root);
      }
      root.seeds.push({ label: r.label, kind: r.kind, keys: r.keys });
      root.keys = [...new Set(root.seeds.flatMap(s => s.keys))];   // combined central centroid
      if (!this.playlistName) this.playlistName = r.label + ' cluster';
      if (fresh) await this.grow(root.id); else await this.refineTree();
    },

    // --- tree growth ---
    // The shared ring fetch: tracks nearest `node`'s pinned-path centroid, pushed off the pruned set,
    // restricted to the genre-family whitelist (#29), minus everything already on the canvas.
    async expandRing(node, k) {
      // 🔥 emphasized tracks are folded into the positive centroid of EVERY grow (doubled for weight),
      // so they steer all subsequent picks toward themselves, independent of which node you grow.
      const pos = this.posKeys(node);
      const pos_keys = this.boosted.length ? [...pos, ...this.boosted, ...this.boosted] : pos;
      try {
        const r = await fetch('/clusters/expand', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          // count_keys = grown, non-pruned tracks only (NOT the central seeds): the per-album cap
          // counts the playlist being built, so a seed artist's album doesn't pre-spend the budget.
          body: JSON.stringify({ pos_keys, neg_keys: this.prunedKeys(), exclude: this.allKeys(),
                                 count_keys: this.keepKeys(), k, allow_genres: this.allowedFamilies,
                                 include_new: true }),   // #48: always reach for new music (toggle removed)
        });
        return (await r.json()).ring || [];
      } catch (e) { return []; }
    },
    // 🔥 emphasis toggle: steers FUTURE grows toward this track; doesn't touch the current canvas.
    isBoosted(n) { return !!n.key && this.boosted.includes(n.key); },
    toggleBoost(n) {
      if (!n.key) return;
      this.boosted = this.boosted.includes(n.key)
        ? this.boosted.filter(key => key !== n.key) : [...this.boosted, n.key];
      this.persist();
    },
    async grow(nodeId) {
      const node = this.nodeById(nodeId);
      if (!node || node.state === 'pruned') return;
      const ring = await this.expandRing(node, 6);
      if (!ring.length) {
        // Nothing left to add. Under a genre filter that means the genre's pool is spent here. Flag
        // the node so its + greys out with an explanation (otherwise it just looks like a dead button).
        if (this.allowedFamilies.length && !this.exhaustedIds.includes(nodeId)) {
          this.exhaustedIds = [...this.exhaustedIds, nodeId];
        }
        return;
      }
      // #30 Growing THROUGH a node marks the path to it as trunk: clicking + on B lights the edge
      // center→B, and growing on down the branch keeps lighting each step. (The centre has no
      // incoming edge, so it never needs to join.)
      if (node.kind !== 'central' && !this.trunk.includes(nodeId)) this.trunk = [...this.trunk, nodeId];
      this.addChildren(node, ring);
    },
    // Re-rank every grown ring against the (now updated) centroids: refine, don't add (#12). Cards
    // you've shaped the tree with survive: pruned markers, drag-pinned cards, and any card you've
    // already grown beneath (a branch). Only the loose leaf cards get swapped for fresher picks, so
    // the card COUNT holds steady while the suggestions tighten around the refined core.
    async refineTree() {
      const parents = this.nodes.filter(n => this.children(n.id).length);
      for (const parent of parents) {
        if (!this.nodeById(parent.id)) continue;                 // a prior iteration may have changed things
        const kids = this.children(parent.id);
        const keep = kids.filter(k => k.state === 'pruned' || k.fx != null || this.children(k.id).length);
        const slots = kids.length - keep.length;
        if (slots <= 0) continue;
        const dropIds = new Set(kids.filter(k => !keep.includes(k)).map(k => k.id));
        this.nodes = this.nodes.filter(n => !dropIds.has(n.id));
        this.trunk = this.trunk.filter(id => !dropIds.has(id));
        if (this.explain && dropIds.has(this.explain.childId)) this.explain = null;
        this.addChildren(parent, await this.expandRing(parent, slots));
      }
    },

    // --- #29 genre whitelist (autosuggest combo: families AND sub-genres) ---
    // The full pick list: coarse families first, then individual genres; a name shown once (a family
    // and a like-named genre collapse to one token, picking it matches either).
    genreOptions() {
      const byName = new Map();
      for (const f of this.families) byName.set(f.family.toLowerCase(), { name: f.family, kind: 'family', n: f.n });
      for (const g of this.genres) {
        const key = g.genre.toLowerCase();
        if (!byName.has(key)) byName.set(key, { name: g.genre, kind: 'genre', n: g.n });
      }
      return [...byName.values()];
    },
    // Options matching what you've typed, minus those already picked (capped for a tidy dropdown).
    genreSuggest() {
      const q = (this.genreQuery || '').trim().toLowerCase();
      return this.genreOptions()
        .filter(o => !this.allowedFamilies.includes(o.name) && (!q || o.name.toLowerCase().includes(q)))
        .slice(0, 10);
    },
    // The genre filter PRUNES off-genre leaves (reversibly) and constrains future grows. It does NOT
    // refetch to refill rings, so the genre's pool stays available for + to grow into.
    _genreChanged() { this.exhaustedIds = []; this.applyGenrePrune(); this.persist(); },   // pool changed; re-prune off-genre
    pickFamily(fam) {
      if (!this.allowedFamilies.includes(fam)) this.allowedFamilies = [...this.allowedFamilies, fam];
      this.genreQuery = ''; this.genreSel = -1;
      this._genreChanged();
    },
    // Arrow-key navigation for the genre combo dropdown.
    genreMove(d) {
      const n = this.genreSuggest().length;
      if (n) this.genreSel = (this.genreSel + d + n) % n;
    },
    genreChoose() {                            // Enter selects the highlighted (or first) suggestion
      const opts = this.genreSuggest();
      const o = opts[this.genreSel >= 0 ? this.genreSel : 0];
      if (o) this.pickFamily(o.name);
    },
    removeFamily(fam) { this.allowedFamilies = this.allowedFamilies.filter(f => f !== fam); this._genreChanged(); },
    popFamily() {                              // Backspace on an empty field removes the last chip
      if (this.allowedFamilies.length) this.allowedFamilies = this.allowedFamilies.slice(0, -1);
      this._genreChanged();
    },
    clearFamilies() { this.allowedFamilies = []; this._genreChanged(); },
    // A track matches the active genre whitelist if a chosen token is its exact genre OR its family.
    // No filter ⇒ everything matches. Untagged tracks never match while a filter is on (#29).
    genreMatches(n) {
      if (!this.allowedFamilies.length || n.kind !== 'track') return true;
      const toks = this.allowedFamilies.map(t => t.toLowerCase());
      return toks.includes((n.genre || '').toLowerCase()) || toks.includes((n.family || '').toLowerCase());
    },
    // The genre filter doesn't HIDE off-genre cards. It PRUNES them (the same struck-through removed
    // state as the ✕), so they stay visible (paths intact), drop out of the save, and you can bring any
    // back with the ✕. Only loose leaves are touched. The trunk and structural nodes are left alone.
    // gpruned marks a prune the FILTER made, so flipping/clearing the filter can undo exactly those
    // (a card you ✕'d or kept by hand stays as you left it).
    applyGenrePrune() {
      for (const n of this.nodes) {
        if (n.kind !== 'track' || this.trunk.includes(n.id) || this.children(n.id).length) continue;
        const match = this.genreMatches(n);
        if (!match && n.state === 'neutral') { n.state = 'pruned'; n.gpruned = true; }
        else if (match && n.state === 'pruned' && n.gpruned) { n.state = 'neutral'; n.gpruned = false; }
      }
    },
    addChildren(parent, ring) {
      if (!ring.length) return;
      // This ring is one sub-cluster; give it its own hue the first time it's grown (#14). Golden-angle
      // by creation order ⇒ consecutive sub-clusters (e.g. down a trunk) step to clearly distinct colours.
      if (!(parent.id in this.subHues)) this.subHues[parent.id] = Math.round((this._subHueN++ * 137.508) % 360);
      // Fan the new ring out evenly and AWAY from the centre so branches radiate outward and the root
      // stays central. The old spawn (cos(i)/sin(i)+40) clustered every ring to one side of its parent;
      // with no centering force the sim settled into that bias and the tree crept off-axis.
      // Outward = the direction this branch is already heading (parent's parent -> parent). The root has
      // no parent, so its first ring spreads across the FULL circle instead of a wedge.
      const gp = parent.parentId != null ? this.nodeById(parent.parentId) : null;
      const outward = gp ? Math.atan2(parent.y - gp.y, parent.x - gp.x) : 0;
      const n = ring.length;
      const spread = gp ? Math.min(Math.PI, n * 0.5) : 2 * Math.PI;   // outward wedge vs. full circle off the root
      ring.forEach((t, i) => {
        const frac = n > 1 ? i / (gp ? n - 1 : n) : 0.5;   // wedge centred on `outward`; circle steps evenly
        const angle = outward + (frac - 0.5) * spread;
        this.nodes.push({
          id: this.nextId++, parentId: parent.id, kind: 'track', key: t.key,
          label: t.title, sub: t.artist, thumbnail: t.thumbnail, vid: t.video_id,
          genre: t.genre || '', family: t.family || '',     // for the genre filter (#29)
          newMusic: !!t.out_of_corpus,                       // #13 P2: not in your library
          state: 'neutral', depth: parent.depth + 1,
          x: parent.x + Math.cos(angle) * LINK_D, y: parent.y + Math.sin(angle) * LINK_D,
        });
      });
      this._reheat();
    },
    prune(id) {
      const n = this.nodeById(id); if (!n || n.kind === 'central') return;
      // Toggling state changes only the card's look, not the layout, so don't restart the sim at
      // all (#14: no jiggle). Un-prune is a pure visual flip. A hand toggle clears gpruned so the
      // genre filter won't silently flip it back.
      if (n.state === 'pruned') { n.state = 'neutral'; n.gpruned = false; this.persist(); return; }
      n.state = 'pruned'; n.gpruned = false;           // pruning terminates the branch...
      this.trunk = this.trunk.filter(t => t !== id);   // ...a pushed-away node is no longer trunk
      const kill = this.descendants(id);              // ...so drop anything grown below it
      if (!kill.size) { this.persist(); return; }      // pruning a leaf: nothing moves, nothing to do
      if (this.explain && kill.has(this.explain.childId)) this.explain = null;
      this.nodes = this.nodes.filter(x => !kill.has(x.id));
      this.trunk = this.trunk.filter(t => !kill.has(t));
      this._syncSim();                                 // drop the removed nodes without re-energizing, no jiggle
    },
    // Clean the canvas down to the spine: soft-prune every track that isn't on the trunk (the path you
    // grew, see grow()) and isn't 🔥-emphasized. The trunk is connected to the centre by construction
    // (you can only grow a child whose parent you already grew), so pruning the loose rings hanging off
    // it leaves the spine intact. A pure state flip, like a hand ✕: reversible per-card, no layout jiggle,
    // nothing removed. (Cascading prune() would delete trunk descendants under a loose parent, so don't.)
    pruneLoose() {
      let changed = false;
      for (const n of this.nodes) {
        if (n.kind !== 'track' || this.trunk.includes(n.id) || this.isBoosted(n)) continue;
        if (n.state !== 'pruned') { n.state = 'pruned'; n.gpruned = false; changed = true; }
      }
      if (changed) this.persist();
    },
    play(n) {                              // open the track on YouTube Music, reusing one named tab
      if (n.vid) window.open('https://music.youtube.com/watch?v=' + n.vid, 'ytPlayerTab');
    },

    // --- derived keys ---
    nodeById(id) { return (this._byId && this._byId.get(id)) || this.nodes.find(n => n.id === id); },
    // Topology (id index + branch id + child/descendant counts) recomputed ONCE per reheat/sync, not per
    // tick/frame. Before this, nodeById was an O(n) scan called inside branchId (per node, per tick) and
    // drawGrid recomputed descendants() for every node every frame: O(n²)+ work that stalled at ~80 nodes.
    _recomputeTopology() {
      const byId = rawColl(new Map()), childArr = rawColl(new Map());
      for (const n of this.nodes) byId.set(n.id, n);
      this._byId = byId;
      // Raw (non-reactive) mirror of the node list for the hot path: the d3 sim and the per-frame
      // renderer read/write x/y/vx/vy here so the physics never goes through Alpine proxies. Elements
      // must be unwrapped one-by-one: `this.nodes = this.nodes.filter(...)` leaves reactive proxies as
      // the array's ELEMENTS, which a top-level Alpine.raw() would not unwrap. Rebuilt only here, i.e.
      // once per structural change, never per frame. The raw object and its proxy share storage, so
      // state set through the proxy (prune/boost) is visible to the sim and vice-versa.
      const A = window.Alpine, raw = rawColl([]), rawById = rawColl(new Map());
      for (const n of this.nodes) { const r = A ? A.raw(n) : n; raw.push(r); rawById.set(r.id, r); }
      this._raw = raw; this._rawById = rawById;
      for (const n of this.nodes) {
        if (n.parentId == null) continue;
        let a = childArr.get(n.parentId); if (!a) { a = []; childArr.set(n.parentId, a); }
        a.push(n);
      }
      const desc = rawColl(new Map());
      const count = (id) => {                 // memoized post-order: each node visited once ⇒ O(n) total
        if (desc.has(id)) return desc.get(id);
        desc.set(id, 0);                       // cycle guard (trees won't, but keep it safe)
        const cs = childArr.get(id); let total = cs ? cs.length : 0;
        if (cs) for (const c of cs) total += count(c.id);
        desc.set(id, total); return total;
      };
      const branch = rawColl(new Map());
      for (const n of this.nodes) {
        count(n.id);
        const b = n.kind === 'central' ? null : this.branchId(n);
        branch.set(n.id, b);
        const r = rawById.get(n.id); if (r) r._b = b;   // stamp branch on the raw node: the separate force reads a._b, not a Map.get per pair
      }
      this._topo = rawColl({ child: childArr, desc, branch });
    },
    childCount(id) { const a = this._topo && this._topo.child.get(id); return a ? a.length : 0; },
    descCount(id) { return (this._topo && this._topo.desc.get(id)) || 0; },
    children(id) { return this.nodes.filter(n => n.parentId === id); },
    // The top-level branch a node belongs to: the first-ring ancestor whose parent is the centre.
    // Used to keep whole branches from overlapping (separate force).
    branchId(n) {
      let cur = n;
      while (cur && cur.parentId != null && cur.parentId !== this.rootId) cur = this.nodeById(cur.parentId);
      return cur ? cur.id : null;
    },
    // #14 colour-coding: a card takes the hue of the SUB-CLUSTER it belongs to, i.e. the grown ring
    // that spawned it, keyed by its parent. Each ring has its own vivid hue (assigned in addChildren),
    // so three sub-clusters down a trunk read as three clearly different colours, not shades of one.
    // Returned as an OBJECT (not a cssText string) so Alpine's :style sets only the hue custom props and
    // leaves the imperatively-written `transform` (the node's position, see _renderFrame) untouched.
    nodeHueVars(n) {
      if (n.kind === 'central' || n.parentId == null) return {};
      const hue = this.subHues[n.parentId];
      if (hue == null) return {};
      return { '--node-hue': String(hue), '--node-sat': '70%', '--node-light': '60%' };
    },
    // #30 The "trunk" is the spine you GROW out (via the + button, see grow()): each node you grow
    // through lights the bright-green line leading into it, so the path you explored glows edge by edge.
    isTrunkNode(id) { return this.trunk.includes(id); },
    // Two SVG <path>s: the faint branches and the bright trunk over them (Alpine can't reliably make
    // per-edge <line> elements inside <svg>, namespace issues, so each is one path on a static node).
    // Both `d`s are built in ONE pass over the raw nodes (see _renderFrame), not bound reactively, so
    // the sim doesn't re-run an all-nodes string build through Alpine on every tick. Trunk membership is
    // a Set, so the split is O(n) rather than O(n·trunk) from a per-node Array.includes.
    _edgePaths() {
      let edge = '', trunk = '';
      const byId = this._rawById, ts = new Set(this.trunk);
      for (const n of (this._raw || [])) {
        if (n.parentId == null) continue;
        const p = byId && byId.get(n.parentId); if (!p) continue;
        const seg = `M${p.x} ${p.y}L${n.x} ${n.y}`;
        if (ts.has(n.id)) trunk += seg; else edge += seg;
      }
      return { edge, trunk };
    },
    // Open the "why this edge?" popover: ask the server to explain the child against its pinned path
    // (the same positive centroid that grew it: central group + every ancestor up the branch).
    async openExplain(childId) {
      const child = this.nodeById(childId); if (!child) return;
      const parent = this.nodeById(child.parentId); if (!parent) return;
      if (this.explain && this.explain.childId === childId) { this.explain = null; return; }  // toggle off
      this.explain = { childId, loading: true, data: null };
      this._scheduleFrame();                 // position the popover now (it isn't a reactive :style binding)
      try {
        const r = await fetch('/clusters/explain', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: child.key, path_keys: this.posKeys(parent) }),
        });
        const data = await r.json();
        if (this.explain && this.explain.childId === childId) this.explain = { childId, loading: false, data };
      } catch (e) {
        if (this.explain && this.explain.childId === childId) this.explain = null;
      }
    },
    closeExplain() { this.explain = null; },
    // Live midpoint of the explained edge, so the popover tracks the node as the sim settles.
    explainAnchor() {
      if (!this.explain) return { x: 0, y: 0 };
      const n = this.nodeById(this.explain.childId); if (!n) return { x: 0, y: 0 };
      const p = this.nodeById(n.parentId); if (!p) return { x: n.x, y: n.y };
      return { x: (p.x + n.x) / 2, y: (p.y + n.y) / 2 };
    },
    centralKeys() { const r = this.rootId != null ? this.nodeById(this.rootId) : null; return r ? r.keys : []; },
    // The seed labels (artist/playlist/song names) you built the cluster from, for its recipe's
    // "Made from" line on save (#15).
    seedLabels() {
      const r = this.rootId != null ? this.nodeById(this.rootId) : null;
      return r ? [...new Set(r.seeds.map(s => s.label))] : [];
    },
    // Only SONG seeds are concrete "central tracks" worth offering to fold into the saved playlist.
    // An artist/playlist seed steers the centroid but isn't a track you explicitly picked.
    centralSongKeys() {
      const r = this.rootId != null ? this.nodeById(this.rootId) : null;
      return r ? r.seeds.filter(s => s.kind === 'song').flatMap(s => s.keys) : [];
    },
    prunedKeys() { return this.nodes.filter(n => n.state === 'pruned').map(n => n.key); },
    keepKeys() {
      return this.nodes.filter(n => n.kind === 'track' && n.state !== 'pruned').map(n => n.key);
    },
    // The spine you grew out: only the trunk nodes (the ones you clicked + on), non-pruned.
    trunkKeys() {
      return this.nodes.filter(n => n.kind === 'track' && n.state !== 'pruned' && this.trunk.includes(n.id))
        .map(n => n.key);
    },
    // What a Save actually writes, per the save-bar toggle.
    saveKeys() { return this.saveMode === 'trunk' ? this.trunkKeys() : this.keepKeys(); },
    // Mirror Home's genOpenYT: on the "Save & play" click (a user gesture, so the popup isn't
    // blocked), open our /generating interstitial in a new tab and stash the handle. The save
    // round-trip can outlive the browser's activation window, so opening from the post-save swap
    // gets blocked; instead generated_result.html (swapped into #cluster-save-result) redirects this
    // tab to the new playlist once it's ready. Stash the saved tracks' thumbnails so the interstitial
    // can orbit them.
    openYTTab() {
      try {
        const keys = new Set(this.saveKeys());
        const thumbs = this.nodes.filter(n => n.thumbnail && keys.has(n.key))
          .map(n => n.thumbnail).slice(0, 16);
        localStorage.setItem('tc_gen_thumbs', JSON.stringify(thumbs));
      } catch (e) {}
      try { window.__ytTab = window.open('/home/generating', '_blank'); } catch (e) {}
    },
    allKeys() {
      const s = new Set(this.centralKeys());
      this.nodes.forEach(n => { if (n.key) s.add(n.key); });
      return [...s];
    },
    // Growing a card IS the positive signal, so a node's children aim at the central group plus
    // every track on the path you grew through to reach it (no separate "pin").
    posKeys(node) {
      const keys = new Set(this.centralKeys());
      for (let cur = node; cur && cur.kind === 'track'; cur = this.nodeById(cur.parentId)) {
        keys.add(cur.key);
      }
      return [...keys];
    },
    descendants(id) {
      const out = new Set(); const stack = [id];
      while (stack.length) {
        const p = stack.pop();
        this.nodes.filter(n => n.parentId === p).forEach(c => { out.add(c.id); stack.push(c.id); });
      }
      return out;
    },

    // --- pan / zoom ---
    worldStyle() { return `transform: translate(${this.tx}px, ${this.ty}px) scale(${this.scale}); transform-origin: 0 0;`; },
    onWheel(e) {
      const rect = e.currentTarget.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      const factor = Math.exp(-e.deltaY * 0.0015);
      const ns = Math.min(2.5, Math.max(0.2, this.scale * factor));
      this.tx = mx - ((mx - this.tx) / this.scale) * ns;
      this.ty = my - ((my - this.ty) / this.scale) * ns;
      this.scale = ns;
      this._scheduleGrid(); this.persist();
    },
    _worldPt(e) {                            // screen → world coords (undo pan + zoom)
      // Reuse the rect captured at drag start: getBoundingClientRect can force a synchronous layout, and
      // pointermove fires many times per drag. The canvas doesn't move mid-drag, so one snapshot is safe.
      const rect = this._dragRect || document.getElementById('cluster-canvas').getBoundingClientRect();
      return { x: (e.clientX - rect.left - this.tx) / this.scale,
               y: (e.clientY - rect.top - this.ty) / this.scale };
    },
    startNodeDrag(n, e) {                    // pointerdown on a card body: grab it, not the canvas
      this._dragRect = document.getElementById('cluster-canvas').getBoundingClientRect();   // snapshot once for the whole drag
      const p = this._worldPt(e);
      this._drag = { id: n.id, moved: false, ox: p.x - n.x, oy: p.y - n.y, sx: e.clientX, sy: e.clientY };
      // capture on the canvas (which owns pointermove/up) so the drag survives the pointer leaving the card
      try { document.getElementById('cluster-canvas').setPointerCapture(e.pointerId); } catch (_) {}
    },
    onPanStart(e) {
      if (e.target.closest('.cluster-node, .cluster-zoombar, .edge-dot, .cluster-explain')) return;   // let nodes/buttons/dots get clicks
      this._pan = { x: e.clientX, y: e.clientY };
      try { e.currentTarget.setPointerCapture(e.pointerId); } catch (_) {}   // keep the drag even over text/cards
    },
    onPanMove(e) {
      if (this._drag) {                      // pointer down on a card
        const n = this.nodeById(this._drag.id); if (!n) return;
        if (!this._drag.moved) {             // stay a CLICK until the pointer travels a few px (#30:
          const dx = e.clientX - this._drag.sx, dy = e.clientY - this._drag.sy;   // so a click reliably
          if (dx * dx + dy * dy < 25) return;                                     // toggles the trunk)
          this._drag.moved = true;
        }
        const p = this._worldPt(e);          // past the threshold: it's a drag, pin under the pointer
        n.fx = p.x - this._drag.ox; n.fy = p.y - this._drag.oy;
        n.x = n.fx; n.y = n.fy;
        // Glue the card to the cursor NOW, on this very pointer event, instead of waiting for a sim tick
        // + render rAF (a frame or two behind = the lag you felt). We also do NOT run the simulation
        // during the drag: the rest of the graph stays put (cheap), and re-settles once on drop. So the
        // drag costs one style write, not a full-graph physics pass per frame.
        const el = this._cardEls && this._cardEls.get(this._drag.id);
        if (el) el.style.transform = `translate(${n.x}px,${n.y}px) translate(-50%,-50%)`;
        this._scheduleFrame();               // let the connecting edges/dots follow within a frame (no physics)
        return;
      }
      if (!this._pan) return;
      this.tx += e.clientX - this._pan.x; this.ty += e.clientY - this._pan.y;
      this._pan = { x: e.clientX, y: e.clientY };
      this._scheduleGrid();
    },
    onPanEnd() {
      if (this._drag) {
        const n = this.nodeById(this._drag.id);
        const wasDrag = this._drag.moved;
        // a pure click (no move) leaves a track free to flow; a real drag pins it where dropped.
        if (n && !this._drag.moved && n.kind !== 'central') { n.fx = null; n.fy = null; }
        this._drag = null; this._dragRect = null;
        // The node STAYS exactly where you dropped it (it's pinned). Do NOT re-run the simulation: a
        // reheat lets the link force drag the whole graph toward the new spot, which reads as the graph
        // "crawling" after every drop. Instead resolve only any overlap the drop created — a synchronous,
        // alpha-free collide pass (see _settleOverlaps) that nudges just the overlapping neighbours apart
        // in a single frame. Edges to the moved node simply stretch; nothing else flows.
        if (wasDrag) { this._settleOverlaps(); this._renderFrame(true); }
      }
      this._pan = null;
      this.persist();                          // save dragged positions / pan offset
    },
    zoomBy(f) {
      const el = document.getElementById('cluster-canvas'); const rect = el.getBoundingClientRect();
      const cx = rect.width / 2, cy = rect.height / 2;
      const ns = Math.min(2.5, Math.max(0.2, this.scale * f));
      this.tx = cx - ((cx - this.tx) / this.scale) * ns;
      this.ty = cy - ((cy - this.ty) / this.scale) * ns;
      this.scale = ns;
      this._scheduleGrid(); this.persist();
    },
    resetView() { this._centerWorld(CENTER, CENTER, 1); },
    _centerWorld(wx, wy, scale) {
      const el = document.getElementById('cluster-canvas'); if (!el) return;
      const rect = el.getBoundingClientRect();
      this.scale = scale;
      this.tx = rect.width / 2 - wx * scale;
      this.ty = rect.height / 2 - wy * scale;
      this._scheduleGrid();
    },
  };
}
