// Clusters tab canvas: the Alpine component behind templates/clusters.html (x-data="clusterCanvas()").
// A pannable/zoomable force graph you seed and grow into a playlist. RENDERING is done by force-graph
// (vendored, global `ForceGraph`) on a single <canvas>: nodes, edges and the warped grid are all drawn
// on the GPU-friendly canvas and the engine renders ON DEMAND (idle = zero repaint). This replaced a
// hand-rolled DOM-cards + 8000px-SVG + per-frame-canvas-grid renderer that repainted the whole viewport
// every frame (the real cause of the shudder). The d3-force simulation + custom forces are kept (run via
// force-graph's .d3Force), so the layout behaviour is unchanged; only the paint path is different.
//
// Node objects must stay OUT of Alpine's reactivity in the hot path: Alpine (Vue) re-proxies any object
// reached through `this`, and force-graph writes x/y/vx/vy on every node every tick. rawColl() marks our
// raw mirror arrays/maps with Vue's __v_skip so reactive() leaves them alone; we hand force-graph the raw
// node objects. State set through the reactive proxy (prune/boost) shares storage with the raw object.
function rawColl(o) { try { Object.defineProperty(o, '__v_skip', { value: true }); } catch (e) {} return o; }

function clusterCanvas() {
  // force-graph centers the graph at the ORIGIN (0,0) — there is no fixed "world" like the old DOM
  // canvas had. The central node is pinned at CENTER, so CENTER must be 0; pinning it at WORLD/2 (the
  // old value) stranded it ~5000px from its own cluster, forcing zoom-to-fit to zoom way out (tiny cards).
  const WORLD = 8000, CENTER = 0;
  const LINK_D = 215, NODE_R = 128;          // spoke length; collision radius
  const {CARD_W, CARD_H, CEN_W} = ClusterGeometry;
  let repaint = () => {};
  const renderer = createClusterRenderer(() => repaint());
  const canvasStorage = ClusterCanvases.open(localStorage);
  return {
    WORLD,
    nodes: [], nextId: 1, rootId: null,
    query: '', results: [], seedSel: -1, genreSel: -1,
    editingSeeds: false, seedBusy: false, randomSeed: Math.floor(Math.random() * 0x100000000),
    clusterError: '', retryAction: null, _seedChangeToken: null, _canvasRevision: 0,
    _searchToken: null, _deepLinkToken: null, _loadStateToken: null,
    _centerHeight: 90,
    _pendingView: null,
    playlistName: '', saveMode: 'all',
    journey: 'auto', journeyName: 'Pick for me', journeyOpen: false,
    explain: null,            // {childId, loading, data} "why this edge?" popover
    hovered: null,            // node id whose action menu (HTML overlay) is showing
    families: [], genres: [], allowedFamilies: [], genreOpen: false, genreQuery: '',
    trunk: [], subHues: {}, _subHueN: 0, exhaustedIds: [], boosted: [],
    _overOverlay: false, _hoverT: 0,      // pointer is over the hover action-menu (keep it open)
    canvases: [], activeId: null,   // saved canvases ({id,label}) and the active one (bottom-right switcher)
    _byId: null, _topo: null, _raw: null, _rawById: null,

    init() {
      const el = this.$refs.graph;
      repaint = () => this._kick();
      const fg = renderer.mount(el, () => this._renderView(), {
        hover: n => this._onHover(n), click: (n, e) => this._click(n, e),
        dragEnd: n => { n.fx = n.x; n.fy = n.y; n._userPinned = true; this._canvasRevision++; this.persist(); },
        frame: () => this._positionOverlays(), zoomEnd: () => this.persist(),
        tick: graph => {
          // Restore the saved camera after force-graph's initial graphData zoom.
          const v = this._pendingView; if (!v) return;
          this._pendingView = null;
          if (v.cx != null) graph.centerAt(v.cx, v.cy, 0);
          if (v.z) graph.zoom(v.z, 0);
        },
        stop: () => { this._revivePointer(); if (this._autofit) { this._autofit = false; this.resetView(); } },
      });
      this._fg = fg;

      this._resize(); window.addEventListener('resize', () => this._resize());
      this._layoutObserver = new ResizeObserver(() => {
        this._resize();
        const h = this.$refs.center.offsetHeight;
        if (h && h !== this._centerHeight) {
          this._centerHeight = h;
          fg.d3Force('collide').radius(n => n.state === 'pruned' ? 44 :
            n.kind === 'central' ? Math.max(NODE_R, h / 2 + 24) : NODE_R);
          this._kick();
        }
      });
      this._layoutObserver.observe(document.getElementById('cluster-canvas'));
      this._layoutObserver.observe(this.$refs.center);
      window.addEventListener('pagehide', () => this._flushState());
      // Mid-edge info dot clicks, intercepted at pointerdown in the CAPTURE phase — BEFORE force-graph's
      // own canvas handler. A dot often sits over a card, where force-graph would otherwise start a node
      // drag (so no click ever fires). Catching it here guarantees the dot opens its "why connected?".
      el.addEventListener('pointerdown', (e) => {
        const d = this._dotAt(e);
        if (d != null) { e.stopImmediatePropagation(); e.preventDefault(); this.openExplain(d); }
      }, true);
      // Show a pointer cursor while hovering a mid-edge info dot. force-graph sets the canvas cursor inline
      // on its own pointermove (fires first, on the canvas); the .dot-hover class wins via !important in CSS.
      el.addEventListener('pointermove', (e) => {
        el.classList.toggle('dot-hover', this._dotAt(e) != null);
      });

      const params = new URLSearchParams(location.search);
      const from = params.get('from'), seed = params.get('seed');
      canvasStorage.migrate();              // fold any single legacy canvas into the multi-canvas set
      this._refreshSwitcher();
      if (from) {
        history.replaceState(null, '', location.pathname);
        // Keep the local canvas mounted while loading. A failed link must not leave
        // an empty graph attached to its storage ID (camera autosaves can run here).
        this._afterInit(this.restore());
        this._loadFrom(from);
      } else if (seed) {
        // Deep-link from the "Explore [X] in your catalog" card: auto-seed and grow a few rings,
        // optionally focused on a genre family, so the user lands on a ready-built cluster. This
        // opens a NEW canvas rather than clobbering whatever was in progress; the bottom-right
        // switcher lets the user jump back to the others.
        history.replaceState(null, '', location.pathname);
        this.newCanvas();
        this._afterInit(false);
        this._deepLink(seed, params.get('genre'), parseInt(params.get('depth') || '2', 10), params.get('label'));
      } else {
        this._afterInit(this.restore());
      }
      fetch('/clusters/genres').then(r => r.json())
        .then(d => { this.families = d.families || []; this.genres = d.genres || []; }).catch(() => {});
    },
    _afterInit(restored) {
      const context = this._graphContext();
      this.$nextTick(() => {
        if (!this._currentGraph(context)) return;
        if (restored) return;                  // saved view is restored in _applyState
        this.resetView();
        this.$refs.seedInput && this.$refs.seedInput.focus();
      });
    },
    async _loadFrom(from) {
      const context = this._graphContext(), token = rawColl({});
      this._loadStateToken = token;
      const current = () => this._loadStateToken === token && this._currentGraph(context);
      try {
        const response = await fetch('/clusters/state/' + encodeURIComponent(from));
        const state = response.ok ? await response.json() : null;
        if (!current()) return;
        const decoded = ClusterCanvases.decode(state, () => Math.floor(Math.random() * 0x100000000));
        if (!decoded) throw new Error('Invalid saved cluster');
        this._flushState();
        const previousId = this.activeId;
        // Reopening a saved playlist always makes a local copy, just like a seed
        // link. Never publish it into an unrelated canvas's persistence identity.
        this._setActive(ClusterCanvases.newId());
        try {
          this._applyState(decoded);
          this._writeState();
        } catch (e) {
          this._loadActive(previousId);
          this.clusterError = 'Could not open the saved cluster.';
          return;
        }
        this._afterInit(true);
      } catch (e) { if (current()) this.clusterError = 'Could not open the saved cluster.'; }
      finally { if (this._loadStateToken === token) this._loadStateToken = null; }
    },
    // Auto-build a cluster from a deep link: name it `label`, focus an optional genre, resolve the
    // seed term to its best library match, add it (root + its ring), then grow a couple more rings.
    // Each ring is allowed to settle before the next is grown (so children land NEXT to their parent
    // rather than being flung out while the layout is mid-flight), and only a few frontier nodes grow
    // per level so the cluster stays tight around the seed instead of sprawling.
    async _deepLink(seed, genre, depth, label) {
      const settle = () => new Promise(res => setTimeout(res, 700));
      const token = rawColl({});
      this._deepLinkToken = token;
      let context;
      const current = () => this._deepLinkToken === token && this._currentGraph(context);
      try {
        if (label) this.playlistName = label;  // name the canvas before addSeed so it sticks (badge)
        // The linked artist/song is the seed; the optional genre only filters its ring.
        if (genre) this.allowedFamilies = [genre];
        this.query = seed;
        context = this._graphContext();
        // Use this request's returned results, never the live suggestion list of another search.
        const results = await this.search();
        if (!current() || !results?.length) return;
        const seeded = await this.addSeed(results[0]); // root + its immediate ring
        if (this._deepLinkToken !== token || !seeded || !this._currentGraph(seeded)) return;
        context = seeded;                     // advance only to a state published by our operation
        for (let lvl = 1; lvl < Math.max(1, depth || 1); lvl++) {
          await settle();
          if (!current()) return;
          const frontier = this.nodes.filter(n => n.depth === lvl && n.state !== 'pruned').slice(0, 3);
          for (const node of frontier) {
            if (!current()) return;
            const grown = await this.grow(node.id);
            if (this._deepLinkToken !== token || !grown || !this._currentGraph(grown)) return;
            context = grown;
          }
        }
        await settle();                        // let the final layout relax before fitting the view
        if (!current()) return;
        this.resetView();
      } catch (e) { /* owned leaf operations report failures; a cancelled link has no side effects */ }
      finally { if (this._deepLinkToken === token) this._deepLinkToken = null; }
    },
    _resize() {
      const el = document.getElementById('cluster-canvas'); if (!el || !this._fg) return;
      const r = el.getBoundingClientRect();
      this._fg.width(r.width).height(r.height);
    },
    // Push the current node/link set into force-graph and let it (re)settle. force-graph keeps each node's
    // x/y by object identity, so a grow spreads the new ring while existing cards barely move.
    _feed() {
      this._recomputeTopology();
      const links = this._raw
        .filter(n => n.parentId != null && this._rawById.has(n.parentId))
        .map(n => ({ source: n.parentId, target: n.id }));
      this._fg.graphData({ nodes: this._raw, links });
      // graphData leaves node hit-testing stale (onNodeHover stops firing, even on the FIRST feed).
      // _revivePointer toggles pointer interaction off→on to fully re-init detection; deferred a frame so it
      // runs after force-graph ingests the data, and again on the next engine stop for a stable re-arm.
      this._kick();
      requestAnimationFrame(() => this._revivePointer());
    },
    // Fully re-initialise force-graph's node hit-testing (stale after every graphData). Safe to call
    // anytime now that links carry no pointer area. Also repaints.
    _revivePointer() { const f = this._fg; if (!f) return; try { f.enablePointerInteraction(false); f.enablePointerInteraction(true); } catch (e) {} this._kick(); },
    // Grow path: re-energize the WHOLE simulation (alpha→1) so neighbouring clusters are pushed away to
    // make room for the new ring (charge + collide + cross-branch separation), instead of the new cards
    // landing on top of what's already there. (Prune uses _syncSim, which doesn't re-energize.)
    _reheat() { this._feed(); this.persist(); },
    _syncSim() { this._feed(); this.persist(); },
    // Force ONE redraw, and revive node hit-testing. resetCountdown does NOT repaint once the engine is
    // idle (verified), and graphData leaves node detection stale; re-setting a node canvas accessor
    // triggers a digest that re-renders the visible + pick canvases, so onNodeHover fires again.
    _kick() { const f = this._fg; if (!f) return; try { f.nodeCanvasObjectMode(f.nodeCanvasObjectMode()); } catch (e) {} },
    // Keep the canvas repainting frame-by-frame while ANY node is 🔥, so the cosmic-fire border animates
    // (the engine renders on demand and otherwise stops). Self-terminates once nothing is boosted.
    _animBoost() {
      if (this._boostRAF) return;
      const loop = () => {
        if (!this.boosted.length) { this._boostRAF = 0; this._kick(); return; }   // one last redraw to clear the ring
        this._kick();
        this._boostRAF = requestAnimationFrame(loop);
      };
      this._boostRAF = requestAnimationFrame(loop);
    },

    _renderView() {
      return {nodes: this._raw || [], byId: this._rawById, topology: this._topo,
        centerHeight: this._centerHeight, subHues: this.subHues, trunk: this.trunk,
        boosted: this.boosted, rootId: this.rootId, hovered: this.hovered, explainChild: this.explain?.childId};
    },
    // The mid-edge info dot nearest a click, or null. Checked on BOTH node and background clicks so the
    // dot wins even when it sits over a card (canvas picks the node, but force-graph hands us the event,
    // so we test proximity ourselves). ~16px screen pick radius, in graph space.
    _dotAt(e) {
      const fg = this._fg, byId = this._rawById; if (!fg || !byId || !e || e.clientX == null) return null;
      // Always derive canvas-relative coords from clientX/Y (offsetX is unreliable across force-graph's
      // event wrapping). The force-graph canvas fills #cluster-canvas, so its rect is the canvas origin.
      const r = document.getElementById('cluster-canvas').getBoundingClientRect();
      let gp = null; try { gp = fg.screen2GraphCoords(e.clientX - r.left, e.clientY - r.top); } catch (_) { return null; }
      return renderer.nearestDot(this._renderView(), gp, 16 / (fg.zoom() || 1));
    },
    _click(node, e) {
      const dot = this._dotAt(e);
      if (dot != null) { this.openExplain(dot); return; }   // clicked a mid-edge dot → "why connected?"
      if (node) { this.hovered = node.id; this._positionOverlays(); }   // clicked a card → action menu
      else { this.explain = null; this.hovered = null; }                // clicked empty space → dismiss
    },
    // --- HTML overlays (action menu + explain popover) tracking graph coords ----------------------
    _onHover(n) {
      clearTimeout(this._hoverT);
      if (n) { this.hovered = n.id; this._positionOverlays(); }   // place it NOW (idle graph isn't rendering, so onRenderFramePost won't)
      else { this._hoverT = setTimeout(() => { if (!this._overOverlay) this.hovered = null; }, 140); }
    },
    _positionOverlays() {
      const fg = this._fg; if (!fg) return;
      const positions = renderer.overlays(this._renderView(), (x, y) => fg.graph2ScreenCoords(x, y), fg.zoom());
      for (const [name, style] of Object.entries(positions)) {
        if (this.$refs[name]) Object.assign(this.$refs[name].style, style);
      }
    },
    hoveredNode() { return this.hovered != null ? this.nodeById(this.hovered) : null; },

    // --- persistence ------------------------------------------------------------------------------
    persist() { clearTimeout(this._persistT); this._persistT = setTimeout(() => { this._persistT = 0; this._writeState(); }, 400); },
    _stateBlob() {
      return ClusterCanvases.snapshot(
        {nodes: this.nodes, nextId: this.nextId, rootId: this.rootId, trunk: this.trunk,
         subHues: this.subHues, subHueN: this._subHueN},
        {allowedFamilies: this.allowedFamilies, boosted: this.boosted,
        playlistName: this.playlistName, saveMode: this.saveMode, journey: this.journey, journeyName: this.journeyName,
         randomSeed: this.randomSeed},
        this._fg ? {z: this._fg.zoom(), cx: (this._fg.centerAt() || {}).x, cy: (this._fg.centerAt() || {}).y} : null);
    },
    clusterStateJSON() { return this.nodes.length ? JSON.stringify(this._stateBlob()) : ''; },
    _writeState() {
      this.activeId = canvasStorage.write(this._stateBlob(), this.activeId);
      this._refreshSwitcher();
    },
    _flushState() { if (this._persistT) { clearTimeout(this._persistT); this._persistT = 0; this._writeState(); } },
    _applyState(s) {
      s = ClusterCanvases.decode(s, () => Math.floor(Math.random() * 0x100000000));
      if (!s) return false;
      this._canvasRevision++;
      this.nodes = s.nodes; this.nextId = s.nextId; this.rootId = s.rootId; this.trunk = s.trunk;
      this.subHues = s.subHues; this._subHueN = s.subHueN;
      this.allowedFamilies = s.allowedFamilies; this.boosted = s.boosted;
      this.playlistName = s.playlistName; this.saveMode = s.saveMode;
      // Older canvases used only the first seed for their automatic name.
      this._syncSeedName((this.centralSeeds()[0]?.label || '') + ' cluster');
      this.journey = s.journey; this.journeyName = s.journeyName;
      this.randomSeed = s.randomSeed;
      this._autofit = false;
      this._pendingView = s.view || null;
      // restore positions without re-layout: pin every node, feed, release tracks after it settles (instant)
      for (const n of this.nodes) { if (n.x != null) { n.fx = n.x; n.fy = n.y; } }
      const restored = this.nodes;
      this._feed();
      this.$nextTick(() => {
        if (this.nodes !== restored) return;
        setTimeout(() => {
          if (this.nodes !== restored) return;
          for (const n of this.nodes) if (n.kind !== 'central' && n.fx != null && !n._userPinned) { n.fx = null; n.fy = null; }
        }, 60);
        if (this.boosted.length) this._animBoost();   // resume the fire animation for restored 🔥 picks
      });
      return true;
    },
    // --- multi-canvas storage (bottom-right switcher) ---------------------------------------------
    // localStorage layout: tc:canvases -> [{id,label}], tc:active -> id, tc:canvas:<id> -> state blob.
    _canvasKey(id) { return ClusterCanvases.key(id); },
    _setActive(id) { this.activeId = id; canvasStorage.setActive(id); },
    _refreshSwitcher() { this.canvases = canvasStorage.list(); this.activeId = canvasStorage.active(); },
    _resetMem() {
      this._canvasRevision++;
      this.nodes = []; this.nextId = 1; this.rootId = null; this.trunk = [];
      this.subHues = {}; this._subHueN = 0; this.exhaustedIds = []; this.boosted = [];
      this.allowedFamilies = []; this.genreQuery = ''; this.genreOpen = false;
      this.query = ''; this.results = []; this.playlistName = ''; this.saveMode = 'all';
      this.editingSeeds = false; this.seedBusy = false;
      this.clusterError = ''; this.retryAction = null; this._seedChangeToken = null;
      this._searchToken = null; this._deepLinkToken = null; this._loadStateToken = null;
      this._autofit = false;
      this._pendingView = null;
      this.randomSeed = Math.floor(Math.random() * 0x100000000);
      this.journey = 'auto'; this.journeyName = 'Pick for me'; this.explain = null; this.hovered = null;
      this._feed();
    },
    restore() {
      if (this.activeId) { const s = canvasStorage.read(this.activeId); if (s) return this._applyState(s); }
      return false;
    },
    _loadActive(id) {
      this._setActive(id); this._resetMem();
      const s = canvasStorage.read(id);
      if (s) this._applyState(s); else this._afterInit(false);
      this._refreshSwitcher();
    },
    switchCanvas(id) {
      if (!id || id === this.activeId) return;
      this._flushState();                    // persist the current canvas before leaving it
      this._loadActive(id);
    },
    newCanvas() {
      this._flushState();                    // persist the current canvas; new one is added on first write
      this._setActive(ClusterCanvases.newId()); this._resetMem(); this._refreshSwitcher();
      this._afterInit(false);
    },
    reset() {
      // "reset" clears the CURRENT canvas, then jumps to another saved one if there is one.
      const cur = this.activeId;
      const list = canvasStorage.remove(cur); this.activeId = null;
      if (list.length) { this._loadActive(list[list.length - 1].id); return; }
      this._resetMem(); this._refreshSwitcher();
      this._afterInit(false);
    },

    // --- search / seeding -------------------------------------------------------------------------
    async search() {
      const q = this.query.trim();
      const context = this._graphContext(), token = rawColl({});
      this._searchToken = token;
      if (!q) { this.results = []; this.seedSel = -1; return []; }
      const current = () => this._searchToken === token && this._currentGraph(context) && this.query.trim() === q;
      try {
        const r = await fetch('/clusters/search?q=' + encodeURIComponent(q));
        if (!r.ok) throw new Error('Could not search');
        const results = await r.json();
        if (!current()) return;
        this.results = results;
        this.seedSel = -1;
        return results;
      } catch (e) {
        if (!current()) return;
        this.results = []; this.seedSel = -1;
        return [];
      }
    },
    seedMove(d) { if (this.results.length) this.seedSel = (this.seedSel + d + this.results.length) % this.results.length; },
    seedChoose() { const r = this.results[this.seedSel >= 0 ? this.seedSel : 0]; if (r) this.addSeed(r); },
    centralSeeds() { const root = this.nodes.find(n => n.id === this.rootId); return root ? root.seeds : []; },
    seedName() { const labels = this.seedLabels(); return labels.length ? labels.join(' + ') + ' cluster' : ''; },
    _syncSeedName(previous = '') {
      if (!this.playlistName || this.playlistName === previous) this.playlistName = this.seedName();
    },
    editSeeds() {
      this.editingSeeds = true;
      const context = this._graphContext();
      this.$nextTick(() => { if (this._currentGraph(context)) this.$refs.seedInput.focus(); });
    },
    async removeSeed(index) {
      return await this._changeSeeds({type: 'remove', index});
    },
    async addSeed(seed) {
      return await this._changeSeeds({type: 'add', seed});
    },
    _curationSignature() {
      // Simulation positions and camera movement can change while requests are in flight.
      // A user's edits to the graph must never be overwritten by an older refinement.
      // The first autosave assigns a storage ID without changing the canvas. Replacement,
      // reset and switching advance _canvasRevision, including a switch back to the same ID.
      return JSON.stringify([this.nodes.map(n =>
        [n.id, n.parentId, n.state, n._userPinned ? [n.fx, n.fy] : null, n.seeds]), this.trunk, this.boosted,
        this.allowedFamilies, this.playlistName, this.randomSeed]);
    },
    _graphContext() { return { revision: this._canvasRevision, signature: this._curationSignature() }; },
    _currentGraph(context) {
      return this._canvasRevision === context.revision && this._curationSignature() === context.signature;
    },
    async _changeSeeds(action) {
      if (this.seedBusy) return;
      const context = this._graphContext(), token = rawColl({});
      this._seedChangeToken = token;
      const current = () => this._seedChangeToken === token && this._currentGraph(context);
      this.seedBusy = true;
      this.clusterError = ''; this.retryAction = null;
      this.query = ''; this.results = [];
      // Refine an isolated draft. Its intermediate feeds/writes never reach the live graph or
      // localStorage, including when a later parent's request fails after earlier ones succeeded.
      try {
        const draft = Object.assign(clusterCanvas(), JSON.parse(JSON.stringify(this._stateBlob())),
          {_subHueN: this._subHueN, exhaustedIds: [...this.exhaustedIds]});
        draft._feed = () => draft._recomputeTopology();
        draft.persist = () => {};
        const expandRing = draft.expandRing.bind(draft);
        draft.expandRing = async (...args) => {
          const ring = await expandRing(...args);
          if (!current()) throw new Error('Refinement cancelled');
          return ring;
        };
        if (action.type === 'add') await draft._addSeed(action.seed);
        else await draft._removeSeed(action.index);
        if (!current()) return;
        this._canvasRevision++;
        // Keep the current simulation positions and any drag pins for surviving nodes.
        for (const n of draft.nodes) {
          const current = this.nodeById(n.id);
          if (current) for (const key of ['x', 'y', 'vx', 'vy', 'fx', 'fy']) n[key] = current[key];
        }
        for (const key of ['nodes', 'nextId', 'rootId', 'trunk', 'subHues', '_subHueN',
                           'allowedFamilies', 'playlistName', 'exhaustedIds']) this[key] = draft[key];
        if (this.explain && !this.nodes.some(n => n.id === this.explain.childId)) this.explain = null;
        this._autofit = true;
        this._reheat();
        return this._graphContext();
      } catch (e) {
        if (current()) {
          this.clusterError = 'Couldn’t update suggestions. Your canvas is still here. Try again.';
          this.retryAction = action;
        }
      } finally {
        if (this._seedChangeToken === token) { this.seedBusy = false; this._seedChangeToken = null; }
      }
    },
    async retryCluster() {
      const action = this.retryAction;
      if (!action || this.seedBusy) return;
      if (action.type === 'grow') await this.grow(action.nodeId);
      else await this._changeSeeds(action);
    },
    async _removeSeed(index) {
      const root = this.nodeById(this.rootId); if (!root) return;
      const previousName = this.seedName();
      const removed = root.seeds.splice(index, 1)[0];
      if (!removed) return;
      if (removed.kind === 'genre') this.allowedFamilies = this.allowedFamilies.filter(f => f !== removed.label);
      root.keys = [...new Set(root.seeds.flatMap(s => s.keys))];
      this._syncSeedName(previousName);
      await this._refineTree();
    },
    async _addSeed(r) {
      if (this.centralSeeds().some(s => s.kind === r.kind && s.label === r.label
        && JSON.stringify(s.keys) === JSON.stringify(r.keys))) return;
      const previousName = this.seedName();
      this.query = ''; this.results = [];
      let root = this.rootId != null ? this.nodeById(this.rootId) : null;
      const fresh = !root;
      if (!root) {
        root = { id: this.nextId++, parentId: null, kind: 'central', state: 'central', depth: 0,
                 seeds: [], keys: [], key: null, vid: null, x: CENTER, y: CENTER, fx: CENTER, fy: CENTER };
        this.rootId = root.id; this.nodes.push(root);
      }
      root.seeds.push({ label: r.label, kind: r.kind, keys: r.keys });
      if (r.kind === 'genre' && !this.allowedFamilies.includes(r.label)) this.allowedFamilies.push(r.label);
      root.keys = [...new Set(root.seeds.flatMap(s => s.keys))];
      this._syncSeedName(previousName);
      this._feed();
      if (fresh || !this.children(root.id).length) await this._grow(root.id);
      else await this._refineTree();
    },

    // --- tree growth ------------------------------------------------------------------------------
    async expandRing(node, k) {
      const pos = this.posKeys(node);
      const pos_keys = this.boosted.length ? [...pos, ...this.boosted, ...this.boosted] : pos;
      const r = await fetch('/clusters/expand', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pos_keys, neg_keys: this.prunedKeys(),
          exclude: node.kind === 'central' ? [...this.canvasKeys(), ...this.centralSongKeys()] : this.allKeys(),
          central_seeds: node.kind === 'central' ? this.centralSeeds() : [], random_seed: this.randomSeed,
          count_keys: this.keepKeys(), k, allow_genres: this.allowedFamilies, include_new: true }),
      });
      if (!r.ok) throw new Error('Could not fetch recommendations');
      const data = await r.json();
      if (!Array.isArray(data?.ring) || data.ring.some(t => !t || typeof t.key !== 'string'
          || !t.key || typeof t.title !== 'string' || typeof t.artist !== 'string')) {
        throw new Error('Invalid recommendation ring');
      }
      return data.ring;
    },
    isBoosted(n) { return !!n && !!n.key && this.boosted.includes(n.key); },
    toggleBoost(n) {
      if (!n || !n.key) return;
      this._canvasRevision++;
      this.boosted = this.boosted.includes(n.key) ? this.boosted.filter(k => k !== n.key) : [...this.boosted, n.key];
      this._kick(); this._animBoost(); this.persist();
    },
    async grow(nodeId) {
      if (this.seedBusy) return;
      const context = this._graphContext();
      try {
        const published = await this._grow(nodeId, context);
        if (published && this._currentGraph(published)) {
          this.clusterError = ''; this.retryAction = null;
          return published;
        }
      } catch (e) {
        if (!this._currentGraph(context)) return;
        this.clusterError = 'Couldn’t load suggestions. Try again.';
        this.retryAction = {type: 'grow', nodeId};
      }
    },
    async _grow(nodeId, context = this._graphContext()) {
      const node = this.nodeById(nodeId);
      if (!node || node.state === 'pruned') return false;
      const ring = await this.expandRing(node, 6);
      if (!this._currentGraph(context)) return false;
      if (!ring.length) {
        if (this.allowedFamilies.length && !this.exhaustedIds.includes(nodeId)) this.exhaustedIds = [...this.exhaustedIds, nodeId];
        return this._graphContext();
      }
      if (node.kind !== 'central' && !this.trunk.includes(nodeId)) this.trunk = [...this.trunk, nodeId];
      this.addChildren(node, ring);
      return this._graphContext();
    },
    async _refineTree() {
      const parents = this.nodes.filter(n => this.children(n.id).length);
      for (const parent of parents) {
        if (!this.nodeById(parent.id)) continue;
        const kids = this.children(parent.id);
        const keep = kids.filter(k => k.state === 'pruned' || k.fx != null || this.isBoosted(k) || this.children(k.id).length);
        const slots = kids.length - keep.length;
        if (slots <= 0) continue;
        const context = this._graphContext();
        const ring = await this.expandRing(parent, slots);
        if (!this._currentGraph(context)) return;
        const dropIds = new Set(kids.filter(k => !keep.includes(k)).map(k => k.id));
        this.nodes = this.nodes.filter(n => !dropIds.has(n.id));
        this.trunk = this.trunk.filter(id => !dropIds.has(id));
        if (this.explain && dropIds.has(this.explain.childId)) this.explain = null;
        this.addChildren(parent, ring);
      }
      this.exhaustedIds = [];
      this._reheat();
    },
    addChildren(parent, ring) {
      if (!ring.length) return;
      this._canvasRevision++;
      const next = ClusterGraph.addChildren({nodes: this.nodes, nextId: this.nextId,
        subHues: this.subHues, subHueN: this._subHueN}, parent, ring, LINK_D);
      this.nodes = next.nodes; this.nextId = next.nextId;
      this.subHues = next.subHues; this._subHueN = next.subHueN;
      this._reheat();
    },
    prune(id) {
      const next = ClusterGraph.prune(this.nodes, this.trunk, id); if (!next) return;
      this._canvasRevision++;
      this._publishNodes(next.nodes); this.trunk = next.trunk;
      if (this.explain && next.removed.has(this.explain.childId)) this.explain = null;
      if (!next.removed.size) { this._recomputeTopology(); this._kick(); this.persist(); return; }
      this._syncSim();
    },
    pruneLoose() {
      let changed = false;
      for (const n of this.nodes) {
        if (n.kind !== 'track' || this.trunk.includes(n.id) || this.isBoosted(n)) continue;
        if (n.state !== 'pruned') { n.state = 'pruned'; n.gpruned = false; changed = true; }
      }
      if (changed) { this._canvasRevision++; this._kick(); this.persist(); }
    },
    play(n) {
      if (!n || !n.vid) return;
      const u = 'https://music.youtube.com/watch?v=' + n.vid;
      if (window.tcPlay) window.tcPlay(u); else window.open(u, 'ytPlayerTab');
    },

    // --- genre whitelist --------------------------------------------------------------------------
    genreOptions() {
      const byName = new Map();
      for (const f of this.families) byName.set(f.family.toLowerCase(), { name: f.family, kind: 'family', n: f.n });
      for (const g of this.genres) { const k = g.genre.toLowerCase(); if (!byName.has(k)) byName.set(k, { name: g.genre, kind: 'genre', n: g.n }); }
      return [...byName.values()];
    },
    genreSuggest() {
      const q = (this.genreQuery || '').trim().toLowerCase();
      return this.genreOptions().filter(o => !this.allowedFamilies.includes(o.name) && (!q || o.name.toLowerCase().includes(q))).slice(0, 10);
    },
    _genreChanged() { this._canvasRevision++; this.exhaustedIds = []; this.applyGenrePrune(); this._kick(); this.persist(); },
    pickFamily(fam) {
      if (this.rootId == null) { this._seedFromGenre(fam); return; }
      if (!this.allowedFamilies.includes(fam)) this.allowedFamilies = [...this.allowedFamilies, fam];
      this.genreQuery = ''; this.genreSel = -1;
      this._genreChanged();
    },
    // Seed a cluster from a genre (no artist/song): a central node tagged with the genre, then a first
    // ring grown from the genre's own centroid (server seed_only path, expandRing with empty pos_keys +
    // allow_genres=[fam]). Same shape as addSeed so save/persist/refine all work.
    async _seedFromGenre(fam) {
      this.genreQuery = ''; this.genreSel = -1;
      await this.addSeed({label: fam, kind: 'genre', keys: []});
    },
    genreMove(d) { const n = this.genreSuggest().length; if (n) this.genreSel = (this.genreSel + d + n) % n; },
    genreChoose() { const o = this.genreSuggest()[this.genreSel >= 0 ? this.genreSel : 0]; if (o) this.pickFamily(o.name); },
    removeFamily(fam) { this.allowedFamilies = this.allowedFamilies.filter(f => f !== fam); this._genreChanged(); },
    popFamily() { if (this.allowedFamilies.length) this.allowedFamilies = this.allowedFamilies.slice(0, -1); this._genreChanged(); },
    clearFamilies() { this.allowedFamilies = []; this._genreChanged(); },
    applyGenrePrune() {
      this._publishNodes(ClusterGraph.genrePrune(this.nodes, this.trunk, this.allowedFamilies));
      this._recomputeTopology();
    },

    // --- topology / derived keys ------------------------------------------------------------------
    nodeById(id) { return (this._byId && this._byId.get(id)) || this.nodes.find(n => n.id === id); },
    _publishNodes(nodes) {
      // Keep surviving simulation objects when committing detached transformations.
      const current = new Map(this.nodes.map(n => [n.id, n]));
      this.nodes = nodes.map(n => { const old = current.get(n.id); return old ? Object.assign(old, n) : n; });
    },
    _recomputeTopology() {
      const topology = ClusterGraph.topology(this.nodes, this.rootId);
      this._byId = rawColl(topology.byId);
      const A = window.Alpine, raw = rawColl([]), rawById = rawColl(new Map());
      for (const n of this.nodes) {
        const r = A ? A.raw(n) : n;
        r._b = topology.branch.get(n.id); raw.push(r); rawById.set(r.id, r);
      }
      this._raw = raw; this._rawById = rawById;
      this._topo = rawColl({child: rawColl(topology.child), desc: rawColl(topology.desc), branch: rawColl(topology.branch)});
    },
    childCount(id) { const a = this._topo && this._topo.child.get(id); return a ? a.length : 0; },
    descCount(id) { return (this._topo && this._topo.desc.get(id)) || 0; },
    children(id) { return this.nodes.filter(n => n.parentId === id); },
    isTrunkNode(id) { return this.trunk.includes(id); },
    // --- explain popover --------------------------------------------------------------------------
    async openExplain(childId) {
      const child = this.nodeById(childId); if (!child) return;
      const parent = this.nodeById(child.parentId); if (!parent) return;
      if (this.explain && this.explain.childId === childId) { this.explain = null; return; }
      const pending = rawColl({ childId, loading: true, data: null });
      const revision = this._canvasRevision;
      this.explain = pending; this._positionOverlays();   // place now (graph may be idle)
      try {
        const r = await fetch('/clusters/explain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: child.key, path_keys: this.posKeys(parent) }) });
        const data = await r.json();
        if (this.explain === pending) this.explain = this._canvasRevision === revision
          ? { childId, loading: false, data } : null;
      } catch (e) { if (this.explain === pending) this.explain = null; }
    },
    closeExplain() { this.explain = null; },

    // --- save keys --------------------------------------------------------------------------------
    centralKeys() { const r = this.rootId != null ? this.nodeById(this.rootId) : null; return r ? r.keys : []; },
    seedLabels() { const r = this.rootId != null ? this.nodeById(this.rootId) : null; return r ? [...new Set(r.seeds.map(s => s.label))] : []; },
    centralSongKeys() { const r = this.rootId != null ? this.nodeById(this.rootId) : null; return r ? r.seeds.filter(s => s.kind === 'song').flatMap(s => s.keys) : []; },
    prunedKeys() { return this.nodes.filter(n => n.state === 'pruned').map(n => n.key); },
    keepKeys() { return this.nodes.filter(n => n.kind === 'track' && n.state !== 'pruned').map(n => n.key); },
    trunkKeys() { return this.nodes.filter(n => n.kind === 'track' && n.state !== 'pruned' && this.trunk.includes(n.id)).map(n => n.key); },
    saveKeys() { return this.saveMode === 'trunk' ? this.trunkKeys() : this.keepKeys(); },
    openYTTab() {
      try { const keys = new Set(this.saveKeys()); const thumbs = this.nodes.filter(n => n.thumbnail && keys.has(n.key)).map(n => n.thumbnail).slice(0, 16); localStorage.setItem('tc_gen_thumbs', JSON.stringify(thumbs)); } catch (e) {}
      try { window.__ytTab = window.open('/home/generating', '_blank'); } catch (e) {}
    },
    allKeys() { const s = new Set(this.centralKeys()); this.nodes.forEach(n => { if (n.key) s.add(n.key); }); return [...s]; },
    canvasKeys() { return this.nodes.filter(n => n.key).map(n => n.key); },
    posKeys(node) { const keys = new Set(this.centralKeys()); for (let cur = node; cur && cur.kind === 'track'; cur = this.nodeById(cur.parentId)) keys.add(cur.key); return [...keys]; },

    // --- view (zoombar) ---------------------------------------------------------------------------
    zoomBy(f) { if (this._fg) this._fg.zoom(Math.min(2.5, Math.max(0.15, this._fg.zoom() * f)), 250); },
    // Fit the whole graph in view (so no branch is left off-screen); fall back to centring on an empty canvas.
    resetView() {
      if (!this._fg) return;
      const nodes = (this._raw || []).filter(n => Number.isFinite(n.x) && Number.isFinite(n.y));
      if (nodes.length) {
        // Include the entire cards, rather than fitting only node centres.
        // Reserve the save bar's actual height, including its wrapped layout on narrow screens.
        let left = Infinity, right = -Infinity, top = Infinity, bottom = -Infinity;
        for (const n of nodes) {
          const w = n.kind === 'central' ? CEN_W : CARD_W, h = n.kind === 'central' ? this._centerHeight : CARD_H;
          left = Math.min(left, n.x - w / 2); right = Math.max(right, n.x + w / 2);
          top = Math.min(top, n.y - h / 2); bottom = Math.max(bottom, n.y + h / 2);
        }
        const bar = this.$el.querySelector('.cluster-savebar');
        const reserve = (bar ? bar.offsetHeight : 60) + 24;
        const z = Math.min(1.2, Math.max(0.15, Math.min((this._fg.width() - 80) / (right - left),
          (this._fg.height() - reserve - 60) / (bottom - top))));
        this._fg.centerAt((left + right) / 2, (top + bottom) / 2 + reserve / (2 * z), 400);
        this._fg.zoom(z, 400);
        return;
      }
      this._fg.centerAt(CENTER, CENTER, 250); this._fg.zoom(1, 250);
    },
  };
}
