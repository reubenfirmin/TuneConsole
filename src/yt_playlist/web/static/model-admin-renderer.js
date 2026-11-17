/* Force simulation and canvas painting for the directed dependency layout. */
window.createModelMapRenderer = function(container, data, events) {
  const START_ZOOM = 1.1, d3 = window.d3;
  const home = {x: 0, y: 0};
  let homeFramed = false;
  const colors = window.themePalette({
    bg: '--bg', surface: '--surface-card', text: '--text', dim: '--dim',
    line: '--border-strong', conditional: '--amber',
  });
  const root = container.closest('#model-map');
  const featureColors = {
    interactive: getComputedStyle(root.querySelector('.mm-key-feature')).color,
    backend: getComputedStyle(root.querySelector('.mm-key-backend')).color,
  };
  const familyColors = new Map([...container.closest('#model-map').querySelectorAll('[data-mm-group]')]
    .map(element => [element.dataset.mmGroup, getComputedStyle(element).color]));
  const {nodes: models, outgoing, direction, separation, featureBoundary, areas} = window.createModelMapLayout(data);
  const areaElements = new Map([...container.closest('#model-map').querySelectorAll('[data-mm-feature-area]')]
    .map(element => [element.dataset.mmFeatureArea, element]));
  let visible = new Set(), featureRoutes = new Map();
  let selected = null, related = new Set(), traced = new Set(), resizeFrame = null;
  const graph = window.ForceGraph()(container);
  const color = node => node.area === 'feature' ? featureColors[node.feature_type] : familyColors.get(node.group) || colors.dim;

  function titleLines(ctx, text, width) {
    const lines = [''];
    for (const word of text.split(' ')) {
      const last = lines.length - 1, next = (lines[last] + ' ' + word).trim();
      if (ctx.measureText(next).width > width - 22 && lines[last]) lines.push(word);
      else lines[last] = next;
    }
    if (lines.length > 2) {
      lines[1] += '…';
      while (ctx.measureText(lines[1]).width > width - 22) lines[1] = lines[1].slice(0, -2) + '…';
    }
    return lines.slice(0, 2);
  }
  function drawNode(node, ctx) {
    ctx.save();
    const feature = node.area === 'feature', backend = node.feature_type === 'backend';
    const width = node.width, height = node.height;
    ctx.globalAlpha = selected && !related.has(node.id) ? .35 : 1;
    const x = node.x - width / 2, y = node.y - height / 2, c = color(node);
    ctx.beginPath();
    if (backend) {
      const cut = 12;
      ctx.moveTo(x + cut, y); ctx.lineTo(x + width - cut, y); ctx.lineTo(x + width, y + cut);
      ctx.lineTo(x + width, y + height - cut); ctx.lineTo(x + width - cut, y + height);
      ctx.lineTo(x + cut, y + height); ctx.lineTo(x, y + height - cut); ctx.lineTo(x, y + cut); ctx.closePath();
    } else ctx.roundRect(x, y, width, height, feature ? height / 2 : 7);
    ctx.fillStyle = feature ? colors.bg : colors.surface; ctx.fill();
    ctx.strokeStyle = c; ctx.lineWidth = node.id === selected ? 2.5 : feature ? 1.6 : 1.2; ctx.stroke();
    if (node.id === selected && !feature) {
      ctx.globalAlpha *= .12; ctx.fillStyle = c; ctx.fill(); ctx.globalAlpha = 1;
    }
    ctx.fillStyle = colors.text;
    ctx.font = feature ? '650 16px "Hanken Grotesk", sans-serif' : '600 15px "Hanken Grotesk", sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const lines = node.labelLines ||= titleLines(ctx, node.title, width);
    lines.forEach((line, index) => ctx.fillText(line, node.x, node.y - (lines.length === 2 ? 12 : 5) + index * 17));
    ctx.fillStyle = c; ctx.font = '500 9px "JetBrains Mono", monospace';
    ctx.fillText(backend ? 'BACKEND' : feature ? 'FEATURE' : node.kind.toUpperCase(), node.x, node.y + 21, width - 18);
    ctx.restore();
  }
  function drawLink(link, ctx) {
    const source = link.source, target = link.target;
    if (!Number.isFinite(source.x) || !Number.isFinite(target.x)) return;
    const a = {x: source.x + source.width / 2 + 4, y: source.y};
    const b = {x: target.x - target.width / 2 - 4, y: target.y};
    const route = featureRoutes.get(target.id), entrance = route ? route[0] : b;
    const bend = Math.max(35, (entrance.x - a.x) * .45);
    const active = traced.has(link.edgeIndex);
    const direct = active && (source.id === selected || target.id === selected);
    ctx.save(); ctx.globalAlpha = active ? .9 : selected ? .04 : .22;
    ctx.strokeStyle = active ? link.condition ? colors.conditional : color(models.get(selected)) : colors.line;
    ctx.fillStyle = ctx.strokeStyle; ctx.lineWidth = active ? direct ? 2.2 : 1.8 : 1;
    if (link.condition) ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(a.x, a.y);
    ctx.bezierCurveTo(a.x + bend, a.y, entrance.x - bend, entrance.y, entrance.x, entrance.y);
    if (route) {
      for (let index = 1; index < route.length - 1; index++) {
        const before = route[index - 1], point = route[index], after = route[index + 1];
        const back = Math.hypot(before.x - point.x, before.y - point.y);
        const forward = Math.hypot(after.x - point.x, after.y - point.y);
        if (!back || !forward) { ctx.lineTo(point.x, point.y); continue; }
        const radius = Math.min(12, back / 2, forward / 2);
        ctx.lineTo(point.x + (before.x - point.x) * radius / back, point.y + (before.y - point.y) * radius / back);
        ctx.quadraticCurveTo(point.x, point.y,
          point.x + (after.x - point.x) * radius / forward, point.y + (after.y - point.y) * radius / forward);
      }
      ctx.lineTo(b.x, b.y);
    }
    ctx.stroke();
    ctx.setLineDash([]);
    const length = active ? 10 : 7;
    ctx.beginPath(); ctx.moveTo(b.x, b.y);
    ctx.lineTo(b.x - length, b.y - length * .5);
    ctx.lineTo(b.x - length, b.y + length * .5);
    ctx.closePath(); ctx.fill(); ctx.restore();
  }

  // Rectangular collision preserves room for labels without wasting circular
  // exclusion areas. This small inventory permits a bounded pairwise pass.
  function collision() {
    let nodes = [];
    const force = () => {
      for (let pass = 0; pass < 2; pass++) for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        const dx = b.x + b.vx - a.x - a.vx, dy = b.y + b.vy - a.y - a.vy;
        const ox = (a.width + b.width) / 2 + 44 - Math.abs(dx);
        const oy = (a.height + b.height) / 2 + 34 - Math.abs(dy);
        if (ox <= 0 || oy <= 0) continue;
        const axis = ox < oy ? 'x' : 'y', overlap = axis === 'x' ? ox : oy;
        const sign = (axis === 'x' ? dx : dy) >= 0 ? -1 : 1;
        const velocity = 'v' + axis, fixed = 'f' + axis;
        const push = overlap * (a[fixed] != null || b[fixed] != null ? .8 : .4) * sign;
        if (a[fixed] == null) a[velocity] += push;
        if (b[fixed] == null) b[velocity] -= push;
      }
    };
    force.initialize = values => { nodes = values; };
    return force;
  }
  function paintHit(node, color, ctx) {
    ctx.fillStyle = color; ctx.fillRect(node.x - node.width / 2, node.y - node.height / 2, node.width, node.height);
  }
  function refreshPointer() {
    // ForceGraph throttles its picking canvas. Flush through the public
    // painter setter after camera/layout changes so clicks use current geometry.
    graph.nodePointerAreaPaint((node, color, ctx) => paintHit(node, color, ctx));
  }
  function positionAreas() {
    const zoom = graph.zoom();
    for (const area of areas) {
      const element = areaElements.get(area.title);
      const members = area.members.filter(node => visible.has(node.id));
      element.hidden = !members.length;
      if (element.hidden) continue;
      const left = Math.min(...members.map(node => node.x - node.width / 2));
      const right = Math.max(...members.map(node => node.x + node.width / 2));
      const top = Math.min(...members.map(node => node.y - node.height / 2));
      const bottom = Math.max(...members.map(node => node.y + node.height / 2));
      area.x = left - 28; area.y = top - 60;
      area.width = right - left + 56; area.height = bottom - top + 88;
      const point = graph.graph2ScreenCoords(area.x, area.y);
      element.style.width = `${area.width}px`; element.style.height = `${area.height}px`;
      element.style.transform = `translate(${point.x}px, ${point.y}px) scale(${zoom})`;
    }
  }
  function frameHome({nodes, links}) {
    if (homeFramed || nodes.length !== models.size) return;
    homeFramed = true;
    const width = graph.width() / START_ZOOM, height = graph.height() / START_ZOOM;
    const center = graph.centerAt();
    const atHome = graph.zoom() === START_ZOOM && Math.hypot(center.x - home.x, center.y - home.y) < 1;
    let nearest = null, distance = Infinity;
    for (const {source, target} of links) {
      if (source.area !== 'model' || target.feature_type !== 'interactive') continue;
      // Keep both endpoints clear of the toolbar and the controls along the bottom.
      const minX = target.x + target.width / 2 - width / 2 + 20 / START_ZOOM;
      const maxX = source.x - source.width / 2 + width / 2 - 20 / START_ZOOM;
      const minY = Math.max(source.y + source.height / 2, target.y + target.height / 2) - height / 2 + 90 / START_ZOOM;
      const maxY = Math.min(source.y - source.height / 2, target.y - target.height / 2) + height / 2 - 140 / START_ZOOM;
      if (maxX < minX || maxY < minY) continue;
      const x = Math.max(minX, Math.min(home.x, maxX)), y = Math.max(minY, Math.min(home.y, maxY));
      const shift = Math.hypot(x - home.x, y - home.y);
      if (shift < distance) { nearest = {x, y}; distance = shift; }
    }
    if (nearest) {
      Object.assign(home, nearest);
      if (atHome && distance > 0) graph.centerAt(home.x, home.y, 0);
    }
  }
  graph.backgroundColor('transparent').nodeId('id').nodeLabel(() => '').linkLabel(() => '')
    .nodeCanvasObject(drawNode).nodeCanvasObjectMode(() => 'replace')
    .nodePointerAreaPaint(paintHit)
    .linkCanvasObject(drawLink).linkCanvasObjectMode(() => 'replace')
    .minZoom(.15).maxZoom(3.5).enableNodeDrag(true)
    .warmupTicks(220).cooldownTicks(90).cooldownTime(2500).d3AlphaDecay(.035).d3VelocityDecay(.5)
    .onNodeHover(node => events.hover(node?.id || null))
    .onNodeClick(node => events.open(node.id))
    .onBackgroundClick(events.background)
    .onNodeDrag(() => { featureRoutes = new Map(); })
    .onZoomEnd(refreshPointer).onNodeDragEnd(refreshPointer)
    .onRenderFramePost(() => { positionAreas(); events.frame(); })
    .onEngineStop(() => {
      const data = graph.graphData();
      featureRoutes = window.routeModelMapFeatures(data.nodes);
      frameHome(data);
      refreshPointer(); container.dataset.settled = 'true';
    });
  graph.d3Force('charge', d3.forceManyBody().strength(-450).distanceMax(700));
  graph.d3Force('center', null);
  graph.d3Force('collision', collision());
  graph.d3Force('clusters', separation);
  graph.d3Force('direction', direction);
  graph.d3Force('feature-boundary', featureBoundary);
  graph.d3Force('depth', d3.forceX(node => node.branchX).strength(node => node.area === 'feature' ? .12 : .025));
  graph.d3Force('branch', d3.forceY(node => node.branchY).strength(node => node.area === 'feature' ? .1 : .025));
  graph.d3Force('link').distance(350)
    .strength(link => link.target.area === 'feature' ? .002 : outgoing.get(link.source.id).length === 1 ? .2 : .06);

  function fit(duration = 350) {
    const values = graph.graphData().nodes;
    if (!values.length) return;
    const left = Math.min(...values.map(n => n.x - n.width / 2)), right = Math.max(...values.map(n => n.x + n.width / 2));
    const top = Math.min(...values.map(n => n.y - n.height / 2)), bottom = Math.max(...values.map(n => n.y + n.height / 2));
    const zoom = Math.max(.15, Math.min(1.2, (graph.width() - 100) / (right - left), (graph.height() - 180) / (bottom - top)));
    graph.centerAt((left + right) / 2, (top + bottom) / 2, duration); graph.zoom(zoom, duration);
  }
  function size() {
    graph.width(container.clientWidth).height(container.clientHeight);
  }
  const resize = new ResizeObserver(() => {
    if (resizeFrame !== null) return;
    resizeFrame = requestAnimationFrame(() => { resizeFrame = null; size(); });
  });
  resize.observe(container); size();
  // An explicit zoom also opts out of ForceGraph's node-count-based auto zoom.
  // Open across the model/feature boundary so consumer cards share the canvas
  // with their model branches, rather than centering the feature band alone.
  if (areas.length) {
    home.x = areas[0].x - graph.width() * .1 / START_ZOOM;
    home.y = areas[0].y + graph.height() / (2 * START_ZOOM) - 110;
  }
  graph.centerAt(home.x, home.y, 0).zoom(START_ZOOM, 0);
  return {
    graph,
    update(ids) {
      visible = ids; featureRoutes = new Map();
      container.dataset.settled = 'false';
      graph.graphData({nodes: [...models.values()].filter(node => ids.has(node.id)),
        links: data.edges.flatMap((edge, edgeIndex) => ids.has(edge.source) && ids.has(edge.target) ? [{...edge, edgeIndex}] : [])});
      positionAreas();
    },
    trace(id, flow) {
      selected = id; related = flow.related; traced = flow.edges;
      graph.nodeCanvasObjectMode(() => 'replace');
    },
    fit,
    resetView() { graph.centerAt(home.x, home.y, 350).zoom(START_ZOOM, 350); },
    center(id) { const node = models.get(id); if (node) graph.centerAt(node.x, node.y, 350); },
    destroy() { resize.disconnect(); if (resizeFrame !== null) cancelAnimationFrame(resizeFrame); graph._destructor(); container.replaceChildren(); },
  };
};
