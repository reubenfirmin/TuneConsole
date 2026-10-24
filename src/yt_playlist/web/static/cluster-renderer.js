// Canvas drawing consumes an explicit view; it owns images, palette and glow caches.
// It never reads the Alpine component, persists state or acquires recommendations.
const ClusterGeometry = Object.freeze({CARD_W: 190, CARD_H: 50, CEN_W: 232, THUMB: 38});
function createClusterRenderer(invalidate) {
  const {CARD_W, CARD_H, CEN_W, THUMB} = ClusterGeometry;
  const images = new Map();
  let palette = null, wellSprite = null, wellRadius = null;
  const C = new Proxy({}, {get: (_, k) => (palette || (palette = window.themePalette({
    bg: '--bg', surface: '--graph-node', border: '--border-2', text: '--text', dim: '--dim',
    accent: '--accent', trunk: '--trunk', danger: '--danger', boost: '--boost',
    chip: '--graph-chip', nodeHi: '--graph-node-hi', node2: '--graph-node-2',
    link: '--graph-link', grid: '--graph-grid', glow: '--graph-glow',
    dotOn: '--graph-dot-on', dotOff: '--graph-dot-off',
    fire0: '--fire-0', fire1: '--fire-1', fire2: '--fire-2', fire3: '--fire-3',
    fire4: '--fire-4', fire5: '--fire-5', fireGlow: '--boost',
  })))[k]});
  function image(url) {
    if (!url) return null;
    let im = images.get(url);
    if (im) return im.ok ? im.el : null;
    const el = new Image(); im = {el, ok: false}; images.set(url, im);
    // Thumbnail hosts need not support CORS. The picking canvas uses only solid rectangles.
    el.onload = () => { im.ok = true; invalidate(); };
    el.onerror = () => { im.ok = false; };
    el.src = url; return null;
  }
  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  function nodeSize(n, ctx, view) {
    if (n.state === 'pruned') {
      ctx.font = '500 9px Hanken Grotesk, sans-serif';
      return {w: Math.max(46, Math.min(150, ctx.measureText(n.label || '').width) + 16), h: 18};
    }
    return n.kind === 'central' ? {w: CEN_W, h: view.centerHeight} : {w: CARD_W, h: CARD_H};
  }
  function clipText(ctx, s, x, y, max) {
    if (!s) return;
    if (ctx.measureText(s).width <= max) { ctx.fillText(s, x, y); return; }
    let lo = 0, hi = s.length;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ctx.measureText(s.slice(0, m) + '…').width <= max) lo = m; else hi = m - 1; }
    ctx.fillText(s.slice(0, lo) + '…', x, y);
  }
  function drawNode(n, ctx, gs, view) {
    const central = n.kind === 'central', {w, h} = nodeSize(n, ctx, view);
    n._dw = w; n._dh = h; // Shared geometry for picking and accessible HTML overlays.
    const x = n.x - w / 2, y = n.y - h / 2;
    if (n.state === 'pruned') {
      ctx.globalAlpha = 0.9;
      roundRect(ctx, x, y, w, h, 5); ctx.fillStyle = C.chip; ctx.fill();
      ctx.strokeStyle = C.danger; ctx.lineWidth = 1; ctx.stroke();
      ctx.fillStyle = C.dim; ctx.font = '500 9px Hanken Grotesk, sans-serif'; ctx.textBaseline = 'middle';
      const lbl = n.label || ''; clipText(ctx, lbl, x + 8, n.y, w - 16);
      const sw = Math.min(w - 16, ctx.measureText(lbl).width);
      ctx.strokeStyle = C.danger; ctx.beginPath(); ctx.moveTo(x + 8, n.y); ctx.lineTo(x + 8 + sw, n.y); ctx.stroke();
      ctx.globalAlpha = 1; ctx.textBaseline = 'top'; return;
    }
    const hue = view.subHues[n.parentId], isTrunk = view.trunk.includes(n.id) && !central;
    let border = central ? C.accent : (hue != null ? `hsl(${hue} 70% 62%)` : C.border);
    if (isTrunk) border = C.trunk;
    ctx.globalAlpha = 1;
    roundRect(ctx, x, y, w, h, 7);
    ctx.fillStyle = central ? C.nodeHi : C.surface; ctx.fill();
    ctx.lineWidth = isTrunk || central ? 2.2 : 1.4; ctx.strokeStyle = border; ctx.stroke();
    if (view.boosted.includes(n.key)) {
      const ang = (performance.now() / 900) % (Math.PI * 2);
      ctx.save(); ctx.globalAlpha = 1;
      let cg = null; try { cg = ctx.createConicGradient(ang, n.x, n.y); } catch (_) {}
      if (cg) {
        cg.addColorStop(0, C.fire0); cg.addColorStop(0.15, C.fire1); cg.addColorStop(0.32, C.fire2);
        cg.addColorStop(0.5, C.fire3); cg.addColorStop(0.62, C.fire4); cg.addColorStop(0.78, C.fire2);
        cg.addColorStop(0.88, C.fire5); cg.addColorStop(1, C.fire0); ctx.strokeStyle = cg;
      } else { ctx.strokeStyle = C.boost; }
      ctx.shadowColor = C.fireGlow; ctx.shadowBlur = 10;
      roundRect(ctx, x - 4, y - 4, w + 8, h + 8, 10); ctx.lineWidth = 3; ctx.stroke(); ctx.restore();
    }
    if (central) return; // The accessible HTML overlay owns wrapped seed labels.
    const tx = x + 6, ty = y + (h - THUMB) / 2, img = image(n.thumbnail);
    ctx.save(); roundRect(ctx, tx, ty, THUMB, THUMB, 4); ctx.clip();
    if (img) ctx.drawImage(img, tx, ty, THUMB, THUMB);
    else { ctx.fillStyle = C.node2; ctx.fillRect(tx, ty, THUMB, THUMB); }
    ctx.restore();
    const txx = tx + THUMB + 8, tw = w - (THUMB + 22);
    ctx.textBaseline = 'top'; ctx.fillStyle = C.text; ctx.font = '600 12px Hanken Grotesk, sans-serif';
    clipText(ctx, n.label || '', txx, y + 8, tw);
    ctx.fillStyle = C.dim; ctx.font = '400 10.5px Hanken Grotesk, sans-serif';
    clipText(ctx, n.sub || '', txx, y + 24, tw);
    if (n.newMusic) { ctx.fillStyle = C.boost; ctx.font = '10px sans-serif'; ctx.fillText('✨', x + w - 14, y + 5); }
    ctx.globalAlpha = 1;
  }
  function nodeHit(n, color, ctx, view) {
    const {w, h} = nodeSize(n, ctx, view);
    ctx.fillStyle = color; ctx.fillRect(n.x - w / 2, n.y - h / 2, w, h);
  }
  function drawDots(ctx, gs, view) {
    if (!view.byId) return;
    for (const n of view.nodes) {
      const p = n.parentId != null ? view.byId.get(n.parentId) : null; if (!p) continue;
      const on = view.explainChild === n.id;
      ctx.beginPath(); ctx.arc((n.x + p.x) / 2, (n.y + p.y) / 2, on ? 7 : 4.5, 0, 7);
      ctx.fillStyle = on ? C.dotOn : C.dotOff; ctx.fill();
      ctx.lineWidth = 1.5 / gs; ctx.strokeStyle = C.bg; ctx.stroke();
    }
  }
  function nearestDot(view, point, radius) {
    let best = null, bestD = radius;
    for (const n of view.nodes) {
      const p = n.parentId != null ? view.byId.get(n.parentId) : null; if (!p) continue;
      const d = Math.hypot(point.x - (n.x + p.x) / 2, point.y - (n.y + p.y) / 2);
      if (d < bestD) { bestD = d; best = n.id; }
    }
    return best;
  }
  function sprite(rad) {
    if (wellSprite && wellRadius === rad) return wellSprite;
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = rad * 2;
    const ctx = canvas.getContext('2d'), gradient = ctx.createRadialGradient(rad, rad, rad * 0.06, rad, rad, rad);
    gradient.addColorStop(0, C.glow); gradient.addColorStop(1, 'transparent');
    ctx.fillStyle = gradient; ctx.beginPath(); ctx.arc(rad, rad, rad, 0, 7); ctx.fill();
    wellSprite = canvas; wellRadius = rad; return canvas;
  }
  function drawGrid(ctx, gs, view, {tl, br}) {
    if (!view.nodes.length) return;
    const GRID = 88, R0 = 320, R0SQ = R0 * R0, PULL = 60, mg = GRID * 2;
    const X0 = Math.floor((tl.x - mg) / GRID) * GRID, X1 = Math.ceil((br.x + mg) / GRID) * GRID;
    const Y0 = Math.floor((tl.y - mg) / GRID) * GRID, Y1 = Math.ceil((br.y + mg) / GRID) * GRID;
    const STEP = Math.max(GRID / 5, (X1 - X0 + Y1 - Y0) / 2 / 240);
    let wells = [];
    for (const n of view.nodes) if (n.kind === 'central' || view.topology?.child.get(n.id)?.length)
      wells.push([n.x, n.y, PULL * ((n.kind === 'central' ? 2.4 : 0.7) + (view.topology?.desc.get(n.id) || 0) * 0.3) * R0SQ]);
    if (wells.length > 16) wells = wells.sort((a, b) => b[2] - a[2]).slice(0, 16);
    const rad = R0 * 0.85, spr = sprite(Math.round(rad));
    ctx.save();
    for (const [x, y] of wells) ctx.drawImage(spr, x - rad, y - rad, rad * 2, rad * 2);
    ctx.lineWidth = 1 / gs; ctx.strokeStyle = C.grid;
    function point(x, y) {
      let dx = 0, dy = 0;
      for (const well of wells) {
        const ex = well[0] - x, ey = well[1] - y, dd = ex * ex + ey * ey, d = Math.sqrt(dd) || 1;
        const ff = well[2] / (dd + R0SQ) / d; dx += ex * ff; dy += ey * ff;
      }
      return [x + dx, y + dy];
    }
    for (let gx = X0; gx <= X1; gx += GRID) {
      ctx.beginPath();
      for (let py = Y0, first = true; py <= Y1; py += STEP) {
        const [x, y] = point(gx, py); if (first) { ctx.moveTo(x, y); first = false; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    for (let gy = Y0; gy <= Y1; gy += GRID) {
      ctx.beginPath();
      for (let px = X0, first = true; px <= X1; px += STEP) {
        const [x, y] = point(px, gy); if (first) { ctx.moveTo(x, y); first = false; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    ctx.restore();
  }
  function overlays(view, project, zoom) {
    const result = {}, root = view.byId?.get(view.rootId);
    if (root) {
      const p = project(root.x, root.y);
      result.center = {transform: `translate(${p.x}px,${p.y}px) translate(-50%, -50%) scale(${zoom})`};
    }
    const hovered = view.hovered != null ? view.byId?.get(view.hovered) : null;
    if (hovered) {
      const p = project(hovered.x, hovered.y);
      const width = hovered._dw || (hovered.kind === 'central' ? CEN_W : CARD_W), height = hovered._dh || CARD_H;
      result.actions = {transform: `translate(${p.x}px,${p.y + (height / 2) * zoom + 3}px) translate(-50%, 0)`,
        width: Math.max(120, width * zoom) + 'px'};
    }
    const child = view.explainChild != null ? view.byId?.get(view.explainChild) : null;
    if (child) {
      const parent = view.byId.get(child.parentId);
      const p = parent ? project((parent.x + child.x) / 2, (parent.y + child.y) / 2) : project(child.x, child.y);
      result.explainEl = {left: p.x + 'px', top: p.y + 'px'};
    }
    return result;
  }
  function mount(el, view, events) {
    const fg = window.ForceGraph()(el), d3 = window.d3;
    const trunk = l => view().trunk.includes(l.target?.id ?? l.target);
    fg.nodeId('id').backgroundColor('transparent')
      .nodeCanvasObject((n, ctx, gs) => drawNode(n, ctx, gs, view()))
      .nodePointerAreaPaint((n, color, ctx) => nodeHit(n, color, ctx, view()))
      .nodeCanvasObjectMode(() => 'replace')
      // Built-in link painting preserves node hit-testing. Edge dots use proximity picking.
      .linkColor(l => trunk(l) ? C.trunk : C.link).linkWidth(l => trunk(l) ? 3 : 1)
      .minZoom(0.15).maxZoom(2.5).enableNodeDrag(true)
      .cooldownTime(2500).d3AlphaDecay(0.1).d3VelocityDecay(0.4)
      .onNodeHover(events.hover).onNodeClick(events.click).onNodeDragEnd(events.dragEnd)
      .onBackgroundClick(e => events.click(null, e))
      .onRenderFramePre((ctx, gs) => {
        let tl, br;
        try { tl = fg.screen2GraphCoords(0, 0); br = fg.screen2GraphCoords(fg.width(), fg.height()); }
        catch (_) { return; }
        drawGrid(ctx, gs, view(), {tl, br});
      })
      .onRenderFramePost((ctx, gs) => { drawDots(ctx, gs, view()); events.frame(); })
      .onZoomEnd(events.zoomEnd).onEngineTick(() => events.tick(fg)).onEngineStop(events.stop);
    fg.d3Force('charge', d3.forceManyBody().strength(-420).distanceMax(1600));
    fg.d3Force('collide', d3.forceCollide(n => n.state === 'pruned' ? 44 :
      n.kind === 'central' ? Math.max(128, view().centerHeight / 2 + 24) : 128).strength(0.95).iterations(2));
    fg.d3Force('separate', ClusterGraph.branchForce());
    const links = fg.d3Force('link');
    if (links) links.distance(l => 215 + Math.min(120, (view().topology?.desc.get(l.target.id) || 0) * 12)).strength(0.7);
    return fg;
  }
  return {mount, nearestDot, overlays};
}
