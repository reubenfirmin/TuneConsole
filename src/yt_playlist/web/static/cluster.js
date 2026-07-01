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
  const CARD_W = 190, CARD_H = 50, CEN_W = 232, THUMB = 38;   // node card geometry (graph units)
  const C = { bg: '#0b0920', surface: '#171340', border: '#3d3582', text: '#ece9ff', dim: '#a59edd',
              accent: '#7c6cff', trunk: '#15e98c', danger: '#ff6b8b', boost: '#ff8a00' };
  return {
    WORLD,
    nodes: [], nextId: 1, rootId: null,
    query: '', results: [], seedSel: -1, genreSel: -1,
    playlistName: '', saveMode: 'all',
    journey: 'auto', journeyName: 'Pick for me', journeyOpen: false,
    explain: null,            // {childId, loading, data} "why this edge?" popover
    hovered: null,            // node id whose action menu (HTML overlay) is showing
    families: [], genres: [], allowedFamilies: [], genreOpen: false, genreQuery: '',
    trunk: [], subHues: {}, _subHueN: 0, exhaustedIds: [], boosted: [],
    _overOverlay: false,      // pointer is over the hover action-menu (keep it open)
    canvases: [], activeId: null,   // saved canvases ({id,label}) and the active one (bottom-right switcher)
    _byId: null, _topo: null, _raw: null, _rawById: null,

    init() {
      const d3 = window.d3, el = this.$refs.graph;
      const fg = window.ForceGraph()(el);
      this._fg = fg;
      this._imgs = new Map();                  // thumbnail cache (url -> HTMLImageElement)
      fg.nodeId('id')
        .backgroundColor('rgba(0,0,0,0)')      // transparent: let the CSS nebula on .cluster-canvas show through
        .nodeCanvasObject((n, ctx, gs) => this._drawNode(n, ctx, gs))
        .nodePointerAreaPaint((n, color, ctx) => this._nodeHit(n, color, ctx))
        .nodeCanvasObjectMode(() => 'replace')
        // Links use force-graph's built-in line drawing (NO custom link canvas/pointer paint): giving
        // links a pointer-pick area silently kills node hit-testing (node hover/click stop firing). The
        // clickable mid-edge "info" dots are drawn by us in onRenderFramePost and clicked via background
        // proximity (see _bgClick→_dotAt), so node interaction is never touched.
        .linkColor(l => this._isTrunkLink(l) ? C.trunk : 'rgba(140,140,170,0.35)')
        .linkWidth(l => this._isTrunkLink(l) ? 3 : 1)
        .minZoom(0.15).maxZoom(2.5)
        .enableNodeDrag(true)
        .cooldownTime(2500).d3AlphaDecay(0.1).d3VelocityDecay(0.4)
        .onNodeHover(n => this._onHover(n))
        .onNodeClick((n, e) => this._click(n, e))   // near a mid-edge dot → explain; else open the action menu
        .onNodeDragEnd(n => { n.fx = n.x; n.fy = n.y; this.persist(); })    // pin where dropped; no graph-wide reheat
        .onBackgroundClick(e => this._click(null, e))   // dot → explain; else dismiss
        .onRenderFramePre((ctx, gs) => this._drawGridBG(ctx, gs))
        .onRenderFramePost((ctx, gs) => { this._drawDots(ctx, gs); this._positionOverlays(); })
        .onEngineStop(() => { this._revivePointer(); if (this._autofit) { this._autofit = false; this.resetView(); } });  // re-arm hit-testing + frame after a settle
      // custom forces, run inside force-graph's d3 simulation.
      // Standard charge to space nodes apart. force-graph's own center force already holds the cloud at the
      // origin, where the central node is pinned (CENTER=0), so NO extra centering force is needed. The only
      // real layout bug was the central node's start coordinate; an added centering force just fights the
      // 'separate' force below and crushes branches back together so their leaves overlap.
      fg.d3Force('charge', d3.forceManyBody().strength(-420).distanceMax(1600));
      // pruned (removed) cards shrink to a minimal pill, so they also claim a far smaller collision
      // radius — they pack in tight and stop hogging space, cutting visual noise.
      fg.d3Force('collide', d3.forceCollide(n => n.state === 'pruned' ? 44 : NODE_R).strength(0.95).iterations(2));
      // centroid separation: pushes whole cluster branches apart so their leaves don't intermingle.
      fg.d3Force('separate', this._clusterForce());
      const lf = fg.d3Force('link');
      if (lf) lf.distance(l => LINK_D + Math.min(120, this.descCount(l.target.id) * 12)).strength(0.7);

      this._resize(); window.addEventListener('resize', () => this._resize());
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
      this._migrateLegacy();                 // fold any single legacy canvas into the multi-canvas set
      this._refreshSwitcher();
      if (from) {
        history.replaceState(null, '', location.pathname);
        fetch('/clusters/state/' + encodeURIComponent(from))
          .then(r => r.ok ? r.json() : null)
          .then(s => this._afterInit(s && this._applyState(s)))
          .catch(() => this._afterInit(false));
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
      this.$nextTick(() => {
        if (restored) return;                  // saved view is restored in _applyState
        this.resetView();
        this.$refs.seedInput && this.$refs.seedInput.focus();
      });
    },
    // Auto-build a cluster from a deep link: name it `label`, focus an optional genre, resolve the
    // seed term to its best library match, add it (root + its ring), then grow a couple more rings.
    // Each ring is allowed to settle before the next is grown (so children land NEXT to their parent
    // rather than being flung out while the layout is mid-flight), and only a few frontier nodes grow
    // per level so the cluster stays tight around the seed instead of sprawling.
    async _deepLink(seed, genre, depth, label) {
      const settle = () => new Promise(res => setTimeout(res, 700));
      try {
        if (label) this.playlistName = label;  // name the canvas before addSeed so it sticks (badge)
        if (genre) this.pickFamily(genre);
        this.query = seed;
        await this.search();
        const r = this.results[0];
        if (!r) return;
        await this.addSeed(r);                 // root + its immediate ring
        for (let lvl = 1; lvl < Math.max(1, depth || 1); lvl++) {
          await settle();
          const frontier = this.nodes.filter(n => n.depth === lvl && n.state !== 'pruned').slice(0, 3);
          for (const node of frontier) await this.grow(node.id);
        }
        await settle();                        // let the final layout relax before fitting the view
        this.resetView();
      } catch (e) { /* deep link is best-effort; leave the user on an empty canvas on failure */ }
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

    // --- node + grid drawing (canvas) -------------------------------------------------------------
    _img(url) {
      if (!url) return null;
      let im = this._imgs.get(url);
      if (im) return im.ok ? im.el : null;
      const el = new Image(); im = { el, ok: false }; this._imgs.set(url, im);
      // NO crossOrigin: thumbnail hosts (i.ytimg.com / googleusercontent) don't all send CORS headers,
      // and 'anonymous' makes those fail to load. We only drawImage them (never read pixels from the
      // visible canvas), and force-graph's hit-test canvas only has solid rects, so tainting is harmless.
      el.onload = () => { im.ok = true; this._kick(); };
      el.onerror = () => { im.ok = false; };
      el.src = url;
      return null;
    },
    _roundRect(ctx, x, y, w, h, r) {
      ctx.beginPath(); ctx.moveTo(x + r, y);
      ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
      ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
    },
    // Drawn geometry of a node (graph units). Pruned cards shrink to a minimal struck-through pill so
    // removed picks fade into the background. Cached on the node (_dw/_dh) for hit-test + overlay placing.
    _nodeSize(n, ctx) {
      if (n.state === 'pruned') { ctx.font = '500 9px Hanken Grotesk, sans-serif'; const tw = Math.min(150, ctx.measureText(n.label || '').width); return { w: Math.max(46, tw + 16), h: 18 }; }
      return n.kind === 'central' ? { w: CEN_W, h: 46 } : { w: CARD_W, h: CARD_H };
    },
    _drawNode(n, ctx, gs) {
      const central = n.kind === 'central';
      const sz = this._nodeSize(n, ctx), w = sz.w, h = sz.h;
      n._dw = w; n._dh = h;                   // cache for _nodeHit + _positionOverlays
      const x = n.x - w / 2, y = n.y - h / 2;
      if (n.state === 'pruned') {             // minimal struck pill (small = quiet)
        ctx.globalAlpha = 0.9;
        this._roundRect(ctx, x, y, w, h, 5); ctx.fillStyle = '#15102e'; ctx.fill();
        ctx.strokeStyle = C.danger; ctx.lineWidth = 1; ctx.stroke();
        ctx.fillStyle = C.dim; ctx.font = '500 9px Hanken Grotesk, sans-serif'; ctx.textBaseline = 'middle';
        const lbl = n.label || ''; this._clipText(ctx, lbl, x + 8, n.y, w - 16);
        const sw = Math.min(w - 16, ctx.measureText(lbl).width);
        ctx.strokeStyle = C.danger; ctx.beginPath(); ctx.moveTo(x + 8, n.y); ctx.lineTo(x + 8 + sw, n.y); ctx.stroke();
        ctx.globalAlpha = 1; ctx.textBaseline = 'top'; return;
      }
      const hue = this.subHues[n.parentId];
      let border = central ? C.accent : (hue != null ? `hsl(${hue} 70% 62%)` : C.border);
      const isTrunk = this.trunk.includes(n.id) && !central;
      if (isTrunk) border = C.trunk;
      ctx.globalAlpha = 1;
      // card body
      this._roundRect(ctx, x, y, w, h, 7);
      ctx.fillStyle = central ? '#221a55' : C.surface; ctx.fill();
      ctx.lineWidth = isTrunk || central ? 2.2 : 1.4;
      ctx.strokeStyle = border; ctx.stroke();
      if (this.boosted.includes(n.key)) {      // 🔥 cosmic-fire emphasis border (animated; see _animBoost)
        const ang = (performance.now() / 900) % (Math.PI * 2);
        ctx.save(); ctx.globalAlpha = 1;
        let cg = null; try { cg = ctx.createConicGradient(ang, n.x, n.y); } catch (e) {}
        if (cg) {
          cg.addColorStop(0, '#c20d00'); cg.addColorStop(0.15, '#ff3000'); cg.addColorStop(0.32, '#ff7a00');
          cg.addColorStop(0.5, '#ffd24a'); cg.addColorStop(0.62, '#fff2c0'); cg.addColorStop(0.78, '#ff7a00');
          cg.addColorStop(0.88, '#b81ad6'); cg.addColorStop(1, '#c20d00');
          ctx.strokeStyle = cg;
        } else { ctx.strokeStyle = C.boost; }
        ctx.shadowColor = 'rgba(255,90,0,0.7)'; ctx.shadowBlur = 10;
        this._roundRect(ctx, x - 4, y - 4, w + 8, h + 8, 10); ctx.lineWidth = 3; ctx.stroke();
        ctx.restore();
      }
      if (central) {
        ctx.globalAlpha = 1;
        ctx.fillStyle = C.accent; ctx.font = '600 8px Hanken Grotesk, sans-serif';
        ctx.textBaseline = 'top'; ctx.fillText('CENTRAL CLUSTER', x + 10, y + 7);
        ctx.fillStyle = C.text; ctx.font = '600 12px Hanken Grotesk, sans-serif';
        const lbl = (n.seeds || []).map(s => s.label).join(' · ');
        this._clipText(ctx, lbl, x + 10, y + 20, w - 20);
        ctx.globalAlpha = 1; return;
      }
      // thumbnail
      const tx = x + 6, ty = y + (h - THUMB) / 2, img = this._img(n.thumbnail);
      ctx.save(); this._roundRect(ctx, tx, ty, THUMB, THUMB, 4); ctx.clip();
      if (img) ctx.drawImage(img, tx, ty, THUMB, THUMB);
      else { ctx.fillStyle = '#241e57'; ctx.fillRect(tx, ty, THUMB, THUMB); }
      ctx.restore();
      // title + artist
      const txx = tx + THUMB + 8, tw = w - (THUMB + 22);
      ctx.textBaseline = 'top';
      ctx.fillStyle = C.text; ctx.font = '600 12px Hanken Grotesk, sans-serif';
      this._clipText(ctx, n.label || '', txx, y + 8, tw);
      ctx.fillStyle = C.dim; ctx.font = '400 10.5px Hanken Grotesk, sans-serif';
      this._clipText(ctx, n.sub || '', txx, y + 24, tw);
      if (n.newMusic) { ctx.fillStyle = C.boost; ctx.font = '10px sans-serif'; ctx.fillText('✨', x + w - 14, y + 5); }
      ctx.globalAlpha = 1;
    },
    _clipText(ctx, s, x, y, max) {
      if (!s) return;
      if (ctx.measureText(s).width <= max) { ctx.fillText(s, x, y); return; }
      let lo = 0, hi = s.length;
      while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ctx.measureText(s.slice(0, m) + '…').width <= max) lo = m; else hi = m - 1; }
      ctx.fillText(s.slice(0, lo) + '…', x, y);
    },
    _nodeHit(n, color, ctx) {
      const sz = this._nodeSize(n, ctx);
      ctx.fillStyle = color; ctx.fillRect(n.x - sz.w / 2, n.y - sz.h / 2, sz.w, sz.h);
    },
    _isTrunkLink(l) { const id = l.target && l.target.id != null ? l.target.id : l.target; return this.trunk.includes(id); },
    // The "mid-vertex info nodes": a clickable dot at each edge's midpoint (the old edge-dot). Drawn on
    // the canvas over the links (onRenderFramePost, graph coords), brighter for the open edge. Clicking is
    // handled in _dotAt/_click by proximity — NOT via a link pointer-area, which would break node hit-testing.
    _drawDots(ctx, gs) {
      const byId = this._rawById; if (!byId) return;
      for (const n of (this._raw || [])) {
        if (n.parentId == null) continue;
        const p = byId.get(n.parentId); if (!p) continue;
        const mx = (n.x + p.x) / 2, my = (n.y + p.y) / 2;
        const on = this.explain && this.explain.childId === n.id;
        ctx.beginPath(); ctx.arc(mx, my, on ? 7 : 4.5, 0, 7);
        ctx.fillStyle = on ? '#a596ff' : '#7d77a8'; ctx.fill();
        ctx.lineWidth = 1.5 / gs; ctx.strokeStyle = C.bg; ctx.stroke();
      }
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
      let best = null, bestD = 16 / (fg.zoom() || 1);
      for (const n of (this._raw || [])) {
        if (n.parentId == null) continue;
        const p = byId.get(n.parentId); if (!p) continue;
        const d = Math.hypot(gp.x - (n.x + p.x) / 2, gp.y - (n.y + p.y) / 2);
        if (d < bestD) { bestD = d; best = n.id; }
      }
      return best;
    },
    _click(node, e) {
      const dot = this._dotAt(e);
      if (dot != null) { this.openExplain(dot); return; }   // clicked a mid-edge dot → "why connected?"
      if (node) { this.hovered = node.id; this._positionOverlays(); }   // clicked a card → action menu
      else { this.explain = null; this.hovered = null; }                // clicked empty space → dismiss
    },
    // Warped "gravity" grid, drawn in GRAPH coordinates inside force-graph's pre-render hook (the ctx is
    // already pan/zoom-transformed). Only runs while the engine is rendering (interaction/settle), so it
    // costs nothing at rest. Wells = the centre + every grown node.
    _drawGridBG(ctx, gs) {
      const fg = this._fg, raw = this._raw; if (!fg || !raw || !raw.length) return;
      const w = fg.width(), h = fg.height();
      let tl, br; try { tl = fg.screen2GraphCoords(0, 0); br = fg.screen2GraphCoords(w, h); } catch (e) { return; }
      const GRID = 88, R0 = 320, R0SQ = R0 * R0, PULL = 60, mg = GRID * 2;
      let X0 = Math.floor((tl.x - mg) / GRID) * GRID, X1 = Math.ceil((br.x + mg) / GRID) * GRID;
      let Y0 = Math.floor((tl.y - mg) / GRID) * GRID, Y1 = Math.ceil((br.y + mg) / GRID) * GRID;
      const STEP = Math.max(GRID / 5, (X1 - X0 + Y1 - Y0) / 2 / 240);   // coarsen when zoomed way out
      let wells = [];
      for (const n of raw) if (n.kind === 'central' || this.childCount(n.id))
        wells.push([n.x, n.y, PULL * ((n.kind === 'central' ? 2.4 : 0.7) + this.descCount(n.id) * 0.3) * R0SQ]);
      if (wells.length > 16) wells = wells.sort((a, b) => b[2] - a[2]).slice(0, 16);
      const wn = wells.length;
      const rad = R0 * 0.85, spr = this._wellSprite(Math.round(rad));
      ctx.save();
      for (let i = 0; i < wn; i++) ctx.drawImage(spr, wells[i][0] - rad, wells[i][1] - rad, rad * 2, rad * 2);
      ctx.lineWidth = 1 / gs; ctx.strokeStyle = 'rgba(74,78,158,0.16)';
      for (let gx = X0; gx <= X1; gx += GRID) {
        ctx.beginPath();
        for (let py = Y0, f = true; py <= Y1; py += STEP) {
          let dx = 0, dy = 0;
          for (let i = 0; i < wn; i++) { const ex = wells[i][0] - gx, ey = wells[i][1] - py, dd = ex * ex + ey * ey, d = Math.sqrt(dd) || 1, ff = wells[i][2] / (dd + R0SQ) / d; dx += ex * ff; dy += ey * ff; }
          if (f) { ctx.moveTo(gx + dx, py + dy); f = false; } else ctx.lineTo(gx + dx, py + dy);
        }
        ctx.stroke();
      }
      for (let gy = Y0; gy <= Y1; gy += GRID) {
        ctx.beginPath();
        for (let px = X0, f = true; px <= X1; px += STEP) {
          let dx = 0, dy = 0;
          for (let i = 0; i < wn; i++) { const ex = wells[i][0] - px, ey = wells[i][1] - gy, dd = ex * ex + ey * ey, d = Math.sqrt(dd) || 1, ff = wells[i][2] / (dd + R0SQ) / d; dx += ex * ff; dy += ey * ff; }
          if (f) { ctx.moveTo(px + dx, gy + dy); f = false; } else ctx.lineTo(px + dx, gy + dy);
        }
        ctx.stroke();
      }
      ctx.restore();
    },
    _wellSprite(rad) {
      if (this._wellSpr && this._wellSprR === rad) return this._wellSpr;
      const c = document.createElement('canvas'); c.width = c.height = rad * 2;
      const g2 = c.getContext('2d'), g = g2.createRadialGradient(rad, rad, rad * 0.06, rad, rad, rad);
      g.addColorStop(0, 'rgba(66,70,150,0.20)'); g.addColorStop(1, 'rgba(66,70,150,0)');
      g2.fillStyle = g; g2.beginPath(); g2.arc(rad, rad, rad, 0, 7); g2.fill();
      this._wellSpr = c; this._wellSprR = rad; return c;
    },

    // --- HTML overlays (action menu + explain popover) tracking graph coords ----------------------
    _onHover(n) {
      clearTimeout(this._hoverT);
      if (n) { this.hovered = n.id; this._positionOverlays(); }   // place it NOW (idle graph isn't rendering, so onRenderFramePost won't)
      else { this._hoverT = setTimeout(() => { if (!this._overOverlay) this.hovered = null; }, 140); }
    },
    _positionOverlays() {
      const fg = this._fg; if (!fg) return;
      if (this.hovered != null && this.$refs.actions) {
        const n = this._rawById && this._rawById.get(this.hovered);
        if (n) {
          // Anchor to the card's BOTTOM edge in screen space (card height scales with zoom, the menu is
          // fixed px), then place the menu's own bottom just inside it — so the buttons sit inside the
          // card across zoom levels instead of drifting off with a fixed offset.
          const p = fg.graph2ScreenCoords(n.x, n.y);
          const z = fg.zoom();
          const cw = n._dw || (n.kind === 'central' ? CEN_W : CARD_W);   // actual drawn size (pruned cards are small)
          const ch = n._dh || CARD_H;
          // pin the menu's TOP just below the card's bottom edge (centered), so it hangs off the bottom
          // and never covers the card's thumbnail/text. Tracks the card+zoom via the drawn size.
          this.$refs.actions.style.transform = `translate(${p.x}px,${p.y + (ch / 2) * z + 3}px) translate(-50%, 0)`;
          // span the card width so the flex:1 buttons stretch into big, easy hit targets
          this.$refs.actions.style.width = Math.max(120, cw * z) + 'px';
        }
      }
      if (this.explain && this.$refs.explainEl) {
        const a = this._explainGraphPt();
        if (a) { const p = fg.graph2ScreenCoords(a.x, a.y); this.$refs.explainEl.style.left = p.x + 'px'; this.$refs.explainEl.style.top = p.y + 'px'; }
      }
    },
    hoveredNode() { return this.hovered != null ? this.nodeById(this.hovered) : null; },

    // --- persistence ------------------------------------------------------------------------------
    persist() { clearTimeout(this._persistT); this._persistT = setTimeout(() => { this._persistT = 0; this._writeState(); }, 400); },
    _stateBlob() {
      return { v: 1, nodes: this.nodes, nextId: this.nextId, rootId: this.rootId, trunk: this.trunk,
        subHues: this.subHues, subHueN: this._subHueN, allowedFamilies: this.allowedFamilies, boosted: this.boosted,
        playlistName: this.playlistName, saveMode: this.saveMode, journey: this.journey, journeyName: this.journeyName,
        view: this._fg ? { z: this._fg.zoom(), cx: (this._fg.centerAt() || {}).x, cy: (this._fg.centerAt() || {}).y } : null };
    },
    clusterStateJSON() { return this.nodes.length ? JSON.stringify(this._stateBlob()) : ''; },
    _writeState() {
      if (!this.nodes.length) {              // active canvas went empty -> drop it from the set
        if (this.activeId) {
          this._saveList(this._loadList().filter(c => c.id !== this.activeId));
          try { localStorage.removeItem(this._canvasKey(this.activeId)); } catch (e) {}
          this._setActive(null);
        }
        this._refreshSwitcher();
        return;
      }
      const id = this._ensureActive();
      try { localStorage.setItem(this._canvasKey(id), JSON.stringify(this._stateBlob())); } catch (e) {}
      const list = this._loadList(), label = this._label(), i = list.findIndex(c => c.id === id);
      if (i >= 0) list[i].label = label; else list.push({ id, label });
      this._saveList(list);
      this._refreshSwitcher();
    },
    _flushState() { if (this._persistT) { clearTimeout(this._persistT); this._persistT = 0; this._writeState(); } },
    _applyState(s) {
      if (!s || s.v !== 1 || !Array.isArray(s.nodes) || !s.nodes.length) return false;
      this.nodes = s.nodes; this.nextId = s.nextId; this.rootId = s.rootId; this.trunk = s.trunk || [];
      this.subHues = s.subHues || {}; this._subHueN = s.subHueN || 0;
      this.allowedFamilies = s.allowedFamilies || []; this.boosted = s.boosted || [];
      this.playlistName = s.playlistName || ''; this.saveMode = s.saveMode || 'all';
      this.journey = s.journey || 'auto'; this.journeyName = s.journeyName || 'Pick for me';
      // restore positions without re-layout: pin every node, feed, release tracks after it settles (instant)
      for (const n of this.nodes) { if (n.x != null) { n.fx = n.x; n.fy = n.y; } }
      this._feed();
      this.$nextTick(() => {
        const v = s.view; if (v && this._fg) { if (v.cx != null) this._fg.centerAt(v.cx, v.cy, 0); if (v.z) this._fg.zoom(v.z, 0); }
        setTimeout(() => { for (const n of this.nodes) if (n.kind !== 'central' && n.fx != null && !n._userPinned) { n.fx = null; n.fy = null; } }, 60);
        if (this.boosted.length) this._animBoost();   // resume the fire animation for restored 🔥 picks
      });
      return true;
    },
    // --- multi-canvas storage (bottom-right switcher) ---------------------------------------------
    // localStorage layout: tc:canvases -> [{id,label}], tc:active -> id, tc:canvas:<id> -> state blob.
    _canvasKey(id) { return 'tc:canvas:' + id; },
    _genId() { return 'c' + Date.now().toString(36) + Math.floor(Math.random() * 46656).toString(36); },
    _loadList() { try { return JSON.parse(localStorage.getItem('tc:canvases')) || []; } catch (e) { return []; } },
    _saveList(list) { try { localStorage.setItem('tc:canvases', JSON.stringify(list)); } catch (e) {} },
    _setActive(id) { this.activeId = id; try { id ? localStorage.setItem('tc:active', id) : localStorage.removeItem('tc:active'); } catch (e) {} },
    _refreshSwitcher() { this.canvases = this._loadList(); this.activeId = (localStorage.getItem('tc:active') || null); },
    _ensureActive() {
      if (this.activeId) return this.activeId;
      const id = this._genId(); this._setActive(id); return id;
    },
    _label() {
      const root = this.rootId != null ? this.nodeById(this.rootId) : null;
      const seed = root && root.seeds && root.seeds[0] ? root.seeds[0].label : '';
      return (this.playlistName || seed || 'Untitled').slice(0, 28);
    },
    // The display label for a saved canvas, read from its stored blob (its name, else its seed).
    _labelFromBlob(s) {
      try {
        const root = (s.nodes || []).find(n => n.id === s.rootId);
        const seed = root && root.seeds && root.seeds[0] ? root.seeds[0].label : '';
        return (s.playlistName || seed || 'Untitled').slice(0, 28);
      } catch (e) { return 'Untitled'; }
    },
    _migrateLegacy() {
      try {
        if (localStorage.getItem('tc:canvases')) { localStorage.removeItem('tc:cluster'); return; }
        const old = localStorage.getItem('tc:cluster');
        const blob = old ? JSON.parse(old) : null;
        if (blob && (blob.nodes || []).length) {
          const id = this._genId();
          localStorage.setItem(this._canvasKey(id), old);
          this._saveList([{ id, label: this._labelFromBlob(blob) }]);
          this._setActive(id);
        }
        localStorage.removeItem('tc:cluster');
      } catch (e) {}
    },
    _resetMem() {
      this.nodes = []; this.nextId = 1; this.rootId = null; this.trunk = [];
      this.subHues = {}; this._subHueN = 0; this.exhaustedIds = []; this.boosted = [];
      this.allowedFamilies = []; this.genreQuery = ''; this.genreOpen = false;
      this.query = ''; this.results = []; this.playlistName = ''; this.saveMode = 'all';
      this.journey = 'auto'; this.journeyName = 'Pick for me'; this.explain = null; this.hovered = null;
      this._feed();
    },
    restore() {
      const id = this.activeId;
      if (id) { let s; try { s = JSON.parse(localStorage.getItem(this._canvasKey(id))); } catch (e) { s = null; } if (s) return this._applyState(s); }
      return false;
    },
    _loadActive(id) {
      this._setActive(id); this._resetMem();
      let s; try { s = JSON.parse(localStorage.getItem(this._canvasKey(id))); } catch (e) { s = null; }
      if (s) this._applyState(s); else this.$nextTick(() => this.resetView());
      this._refreshSwitcher();
    },
    switchCanvas(id) {
      if (!id || id === this.activeId) return;
      this._flushState();                    // persist the current canvas before leaving it
      this._loadActive(id);
    },
    newCanvas() {
      this._flushState();                    // persist the current canvas; new one is added on first write
      this._setActive(this._genId()); this._resetMem(); this._refreshSwitcher();
      this.$nextTick(() => { this.resetView(); this.$refs.seedInput && this.$refs.seedInput.focus(); });
    },
    reset() {
      // "reset" clears the CURRENT canvas, then jumps to another saved one if there is one.
      const cur = this.activeId;
      const list = this._loadList().filter(c => c.id !== cur);
      if (cur) { try { localStorage.removeItem(this._canvasKey(cur)); } catch (e) {} }
      this._saveList(list); this._setActive(null);
      if (list.length) { this._loadActive(list[list.length - 1].id); return; }
      this._resetMem(); this._refreshSwitcher();
      this.$nextTick(() => { this.resetView(); this.$refs.seedInput && this.$refs.seedInput.focus(); });
    },

    // --- search / seeding -------------------------------------------------------------------------
    async search() {
      const q = this.query.trim();
      if (!q) { this.results = []; this.seedSel = -1; return; }
      try { const r = await fetch('/clusters/search?q=' + encodeURIComponent(q)); this.results = await r.json(); }
      catch (e) { this.results = []; }
      this.seedSel = -1;
    },
    seedMove(d) { if (this.results.length) this.seedSel = (this.seedSel + d + this.results.length) % this.results.length; },
    seedChoose() { const r = this.results[this.seedSel >= 0 ? this.seedSel : 0]; if (r) this.addSeed(r); },
    async addSeed(r) {
      this.query = ''; this.results = [];
      let root = this.rootId != null ? this.nodeById(this.rootId) : null;
      const fresh = !root;
      if (!root) {
        root = { id: this.nextId++, parentId: null, kind: 'central', state: 'central', depth: 0,
                 seeds: [], keys: [], key: null, vid: null, x: CENTER, y: CENTER, fx: CENTER, fy: CENTER };
        this.rootId = root.id; this.nodes.push(root);
      }
      root.seeds.push({ label: r.label, kind: r.kind, keys: r.keys });
      root.keys = [...new Set(root.seeds.flatMap(s => s.keys))];
      if (!this.playlistName) this.playlistName = r.label + ' cluster';
      if (fresh) { this._autofit = true; await this.grow(root.id); } else await this.refineTree();   // frame the cluster on the first add only
    },

    // --- tree growth ------------------------------------------------------------------------------
    async expandRing(node, k) {
      const pos = this.posKeys(node);
      const pos_keys = this.boosted.length ? [...pos, ...this.boosted, ...this.boosted] : pos;
      try {
        const r = await fetch('/clusters/expand', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pos_keys, neg_keys: this.prunedKeys(), exclude: this.allKeys(),
            count_keys: this.keepKeys(), k, allow_genres: this.allowedFamilies, include_new: true }),
        });
        return (await r.json()).ring || [];
      } catch (e) { return []; }
    },
    isBoosted(n) { return !!n && !!n.key && this.boosted.includes(n.key); },
    toggleBoost(n) {
      if (!n || !n.key) return;
      this.boosted = this.boosted.includes(n.key) ? this.boosted.filter(k => k !== n.key) : [...this.boosted, n.key];
      this._kick(); this._animBoost(); this.persist();
    },
    async grow(nodeId) {
      const node = this.nodeById(nodeId);
      if (!node || node.state === 'pruned') return;
      const ring = await this.expandRing(node, 6);
      if (!ring.length) {
        if (this.allowedFamilies.length && !this.exhaustedIds.includes(nodeId)) this.exhaustedIds = [...this.exhaustedIds, nodeId];
        return;
      }
      if (node.kind !== 'central' && !this.trunk.includes(nodeId)) this.trunk = [...this.trunk, nodeId];
      this.addChildren(node, ring);
    },
    async refineTree() {
      const parents = this.nodes.filter(n => this.children(n.id).length);
      for (const parent of parents) {
        if (!this.nodeById(parent.id)) continue;
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
    addChildren(parent, ring) {
      if (!ring.length) return;
      if (!(parent.id in this.subHues)) this.subHues[parent.id] = Math.round((this._subHueN++ * 137.508) % 360);
      const gp = parent.parentId != null ? this.nodeById(parent.parentId) : null;
      const outward = gp ? Math.atan2(parent.y - gp.y, parent.x - gp.x) : 0;
      const n = ring.length, spread = gp ? Math.min(Math.PI, n * 0.5) : 2 * Math.PI;
      ring.forEach((t, i) => {
        const frac = n > 1 ? i / (gp ? n - 1 : n) : 0.5, angle = outward + (frac - 0.5) * spread;
        this.nodes.push({ id: this.nextId++, parentId: parent.id, kind: 'track', key: t.key,
          label: t.title, sub: t.artist, thumbnail: t.thumbnail, vid: t.video_id,
          genre: t.genre || '', family: t.family || '', newMusic: !!t.out_of_corpus,
          state: 'neutral', depth: parent.depth + 1,
          x: parent.x + Math.cos(angle) * LINK_D, y: parent.y + Math.sin(angle) * LINK_D });
      });
      this._reheat();
    },
    prune(id) {
      const n = this.nodeById(id); if (!n || n.kind === 'central') return;
      if (n.state === 'pruned') { n.state = 'neutral'; n.gpruned = false; this._kick(); this.persist(); return; }
      n.state = 'pruned'; n.gpruned = false;
      this.trunk = this.trunk.filter(t => t !== id);
      const kill = this.descendants(id);
      if (!kill.size) { this._kick(); this.persist(); return; }
      if (this.explain && kill.has(this.explain.childId)) this.explain = null;
      this.nodes = this.nodes.filter(x => !kill.has(x.id));
      this.trunk = this.trunk.filter(t => !kill.has(t));
      this._syncSim();
    },
    pruneLoose() {
      let changed = false;
      for (const n of this.nodes) {
        if (n.kind !== 'track' || this.trunk.includes(n.id) || this.isBoosted(n)) continue;
        if (n.state !== 'pruned') { n.state = 'pruned'; n.gpruned = false; changed = true; }
      }
      if (changed) { this._kick(); this.persist(); }
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
    _genreChanged() { this.exhaustedIds = []; this.applyGenrePrune(); this._kick(); this.persist(); },
    pickFamily(fam) {
      if (!this.allowedFamilies.includes(fam)) this.allowedFamilies = [...this.allowedFamilies, fam];
      this.genreQuery = ''; this.genreSel = -1;
      if (this.rootId == null) this._seedFromGenre(fam);   // genre alone SEEDS a cluster; seed+genre just filters
      else this._genreChanged();
    },
    // Seed a cluster from a genre (no artist/song): a central node tagged with the genre, then a first
    // ring grown from the genre's own centroid (server seed_only path, expandRing with empty pos_keys +
    // allow_genres=[fam]). Same shape as addSeed so save/persist/refine all work.
    async _seedFromGenre(fam) {
      this._autofit = true;
      const root = { id: this.nextId++, parentId: null, kind: 'central', state: 'central', depth: 0,
        seeds: [{ label: fam, kind: 'genre', keys: [] }], keys: [], key: null, vid: null,
        x: CENTER, y: CENTER, fx: CENTER, fy: CENTER };
      this.rootId = root.id; this.nodes.push(root);
      if (!this.playlistName) this.playlistName = fam + ' cluster';
      await this.grow(root.id);
      this.exhaustedIds = []; this.persist();
    },
    genreMove(d) { const n = this.genreSuggest().length; if (n) this.genreSel = (this.genreSel + d + n) % n; },
    genreChoose() { const o = this.genreSuggest()[this.genreSel >= 0 ? this.genreSel : 0]; if (o) this.pickFamily(o.name); },
    removeFamily(fam) { this.allowedFamilies = this.allowedFamilies.filter(f => f !== fam); this._genreChanged(); },
    popFamily() { if (this.allowedFamilies.length) this.allowedFamilies = this.allowedFamilies.slice(0, -1); this._genreChanged(); },
    clearFamilies() { this.allowedFamilies = []; this._genreChanged(); },
    genreMatches(n) {
      if (!this.allowedFamilies.length || n.kind !== 'track') return true;
      const toks = this.allowedFamilies.map(t => t.toLowerCase());
      return toks.includes((n.genre || '').toLowerCase()) || toks.includes((n.family || '').toLowerCase());
    },
    applyGenrePrune() {
      for (const n of this.nodes) {
        if (n.kind !== 'track' || this.trunk.includes(n.id) || this.children(n.id).length) continue;
        const match = this.genreMatches(n);
        if (!match && n.state === 'neutral') { n.state = 'pruned'; n.gpruned = true; }
        else if (match && n.state === 'pruned' && n.gpruned) { n.state = 'neutral'; n.gpruned = false; }
      }
    },

    // --- topology / derived keys ------------------------------------------------------------------
    nodeById(id) { return (this._byId && this._byId.get(id)) || this.nodes.find(n => n.id === id); },
    _recomputeTopology() {
      const byId = rawColl(new Map()), childArr = rawColl(new Map());
      for (const n of this.nodes) byId.set(n.id, n);
      this._byId = byId;
      const A = window.Alpine, raw = rawColl([]), rawById = rawColl(new Map());
      for (const n of this.nodes) { const r = A ? A.raw(n) : n; raw.push(r); rawById.set(r.id, r); }
      this._raw = raw; this._rawById = rawById;
      for (const n of this.nodes) { if (n.parentId == null) continue; let a = childArr.get(n.parentId); if (!a) { a = []; childArr.set(n.parentId, a); } a.push(n); }
      const desc = rawColl(new Map());
      const count = (id) => { if (desc.has(id)) return desc.get(id); desc.set(id, 0); const cs = childArr.get(id); let t = cs ? cs.length : 0; if (cs) for (const c of cs) t += count(c.id); desc.set(id, t); return t; };
      const branch = rawColl(new Map());
      for (const n of this.nodes) { count(n.id); const b = n.kind === 'central' ? null : this.branchId(n); branch.set(n.id, b); const r = rawById.get(n.id); if (r) r._b = b; }
      this._topo = rawColl({ child: childArr, desc, branch });
    },
    childCount(id) { const a = this._topo && this._topo.child.get(id); return a ? a.length : 0; },
    descCount(id) { return (this._topo && this._topo.desc.get(id)) || 0; },
    children(id) { return this.nodes.filter(n => n.parentId === id); },
    branchId(n) { let cur = n; while (cur && cur.parentId != null && cur.parentId !== this.rootId) cur = this.nodeById(cur.parentId); return cur ? cur.id : null; },
    isTrunkNode(id) { return this.trunk.includes(id); },
    // Cross-branch separation force, at the CENTROID level (not node-pairs): each top-level branch (a
    // first-ring child of the centre and everything under it, keyed by n._b) is treated as ONE blob. We
    // compute each branch's centroid + an approximate radius (∝ √node-count), and when two centroids are
    // closer than the sum of their radii we push the WHOLE branches apart along the centroid-to-centroid
    // axis. Moving branches as units keeps distinct clusters in their own regions instead of letting their
    // cards interleave; per-card overlap is still handled separately by forceCollide. O(n + branches²).
    _clusterForce() {
      const self = this; let nodes = [];
      function force(alpha) {
        if (!self._topo) return;
        const k = alpha * 0.9;
        const sx = new Map(), sy = new Map(), cnt = new Map();   // per-branch position sums + counts
        for (const n of nodes) {
          const b = n._b; if (b == null) continue;               // skip the pinned centre
          sx.set(b, (sx.get(b) || 0) + n.x); sy.set(b, (sy.get(b) || 0) + n.y); cnt.set(b, (cnt.get(b) || 0) + 1);
        }
        const bs = [...cnt.keys()]; if (bs.length < 2) return;
        const cx = new Map(), cy = new Map(), rad = new Map();
        for (const b of bs) { const c = cnt.get(b); cx.set(b, sx.get(b) / c); cy.set(b, sy.get(b) / c); rad.set(b, 150 + Math.sqrt(c) * 115); }
        const dx = new Map(), dy = new Map();                    // accumulated push per branch
        for (let i = 0; i < bs.length; i++) {
          const bi = bs[i], xi = cx.get(bi), yi = cy.get(bi);
          for (let j = i + 1; j < bs.length; j++) {
            const bj = bs[j];
            let ex = xi - cx.get(bj), ey = yi - cy.get(bj), d = Math.hypot(ex, ey) || 1;
            const want = rad.get(bi) + rad.get(bj);              // desired clearance between the two blobs
            if (d < want) {
              if (d < 1) { ex = (i - j) || 1; ey = ((i + 1) * (j + 2)) % 3 - 1 || 1; d = Math.hypot(ex, ey); }
              // push the two blobs apart along their centroid axis; (want-d)→0 as they reach clearance,
              // so it converges. Strong enough (k=alpha·0.9) to fully de-intermingle within the settle.
              const push = (want - d) / d * k * 0.5;
              dx.set(bi, (dx.get(bi) || 0) + ex * push); dy.set(bi, (dy.get(bi) || 0) + ey * push);
              dx.set(bj, (dx.get(bj) || 0) - ex * push); dy.set(bj, (dy.get(bj) || 0) - ey * push);
            }
          }
        }
        for (const n of nodes) { const b = n._b; if (b == null) continue; const px = dx.get(b); if (px) { n.vx += px; n.vy += dy.get(b); } }
      }
      force.initialize = (n) => { nodes = n; };
      return force;
    },

    // --- explain popover --------------------------------------------------------------------------
    async openExplain(childId) {
      const child = this.nodeById(childId); if (!child) return;
      const parent = this.nodeById(child.parentId); if (!parent) return;
      if (this.explain && this.explain.childId === childId) { this.explain = null; return; }
      this.explain = { childId, loading: true, data: null }; this._positionOverlays();   // place now (graph may be idle)
      try {
        const r = await fetch('/clusters/explain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: child.key, path_keys: this.posKeys(parent) }) });
        const data = await r.json();
        if (this.explain && this.explain.childId === childId) this.explain = { childId, loading: false, data };
      } catch (e) { if (this.explain && this.explain.childId === childId) this.explain = null; }
    },
    closeExplain() { this.explain = null; },
    _explainGraphPt() {
      if (!this.explain) return null;
      const n = this._rawById && this._rawById.get(this.explain.childId); if (!n) return null;
      const p = this._rawById.get(n.parentId); if (!p) return { x: n.x, y: n.y };
      return { x: (p.x + n.x) / 2, y: (p.y + n.y) / 2 };
    },

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
    posKeys(node) { const keys = new Set(this.centralKeys()); for (let cur = node; cur && cur.kind === 'track'; cur = this.nodeById(cur.parentId)) keys.add(cur.key); return [...keys]; },
    descendants(id) { const out = new Set(), stack = [id]; while (stack.length) { const p = stack.pop(); this.nodes.filter(n => n.parentId === p).forEach(c => { out.add(c.id); stack.push(c.id); }); } return out; },

    // --- view (zoombar) ---------------------------------------------------------------------------
    zoomBy(f) { if (this._fg) this._fg.zoom(Math.min(2.5, Math.max(0.15, this._fg.zoom() * f)), 250); },
    // Fit the whole graph in view (so no branch is left off-screen); fall back to centring on an empty canvas.
    resetView() {
      if (!this._fg) return;
      if (this.nodes.length > 1) { try { this._fg.zoomToFit(400, 80); return; } catch (e) {} }
      this._fg.centerAt(CENTER, CENTER, 250); this._fg.zoom(1, 250);
    },
  };
}
