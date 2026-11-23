/* Force simulation and canvas painting for the directed dependency layout. */
window.createModelMapRenderer = function(container, data, events) {
  const START_ZOOM = 1.1, d3 = window.d3;
  const home = {x: 0, y: 0, zoom: START_ZOOM};
  let homeFramed = false;
  const colors = window.themePalette({
    bg: '--bg', surface: '--surface-card', text: '--text', dim: '--dim',
    line: '--border-strong', conditional: '--amber', feedback: '--teal',
  });
  const root = container.closest('#model-map');
  const featureColors = {
    interactive: getComputedStyle(root.querySelector('.mm-key-feature')).color,
    backend: getComputedStyle(root.querySelector('.mm-key-backend')).color,
  };
  const familyColors = new Map([...container.closest('#model-map').querySelectorAll('[data-mm-group]')]
    .map(element => [element.dataset.mmGroup, getComputedStyle(element).color]));
  const traceColors = [...root.querySelectorAll('.mm-trace-key i')].map(element => getComputedStyle(element).color);
  const traceWeights = [[1, 3.2], [.86, 2.3], [.68, 1.8], [.35, 1.35]];
  const {nodes: models, outgoing, direction, separation, featurePlacement, areas, tasteLayers} = window.createModelMapLayout(data);
  const firstLayer = tasteLayers[0], lastLayer = tasteLayers.at(-1);
  const layerIndex = new Map(tasteLayers.map((node, index) => [node.id, index]));
  const layerLeft = firstLayer.x - firstLayer.width / 2, layerRight = lastLayer.x + lastLayer.width / 2;
  const areaElements = new Map([...root.querySelectorAll('[data-mm-feature-area]')]
    .map(element => [element.dataset.mmFeatureArea, {element, heading: element.querySelector('h2')}]));
  let visible = new Set(), linkRoutes = new Map(), tasteInputs = new Map();
  let selected = null, related = new Set(), traced = new Map(), resizeFrame = null;
  const graph = window.ForceGraph()(container);
  const color = node => node.area === 'feature' ? featureColors[node.feature_type] : familyColors.get(node.group) || colors.dim;

  function drawTasteLayers(ctx) {
    if (!tasteLayers.every(node => visible.has(node.id))) return;
    ctx.save();
    ctx.strokeStyle = color(firstLayer);
    ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    for (const node of tasteLayers.slice(0, -1)) {
      const x = node.x + node.width / 2 + 23, y = node.y;
      ctx.beginPath(); ctx.moveTo(x - 5, y - 6); ctx.lineTo(x + 2, y); ctx.lineTo(x - 5, y + 6); ctx.stroke();
    }
    ctx.restore();
  }

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
    ctx.strokeStyle = c; ctx.lineWidth = node.id === selected ? 2.5 : feature || node.tasteLayer ? 1.6 : 1.2; ctx.stroke();
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
  function linkStyle(link, ctx, withinTaste) {
    const hop = traced.get(link.edgeIndex);
    if (hop) {
      const level = Math.min(hop, 4) - 1;
      ctx.strokeStyle = traceColors[level];
      [ctx.globalAlpha, ctx.lineWidth] = traceWeights[level];
    } else {
      ctx.strokeStyle = link.feedback ? colors.feedback : link.condition ? colors.conditional
        : withinTaste ? color(firstLayer) : colors.line;
      ctx.globalAlpha = selected ? .04 : withinTaste ? .55 : link.feedback ? .25 : .22;
      ctx.lineWidth = withinTaste ? 1.5 : 1;
    }
    ctx.fillStyle = ctx.strokeStyle;
    if (link.feedback) ctx.setLineDash([2, 5]);
    else if (link.condition) ctx.setLineDash([5, 4]);
    return hop === 1 ? 11 : hop ? 9 : 7;
  }

  function drawLink(link, ctx) {
    const source = link.source, target = link.target;
    if (!Number.isFinite(source.x) || !Number.isFinite(target.x)) return;
    const withinTaste = source.tasteLayer && target.tasteLayer;
    ctx.save();
    const length = linkStyle(link, ctx, withinTaste);
    if (link.feedback) {
      const a = {x: source.x - source.width / 2 - 4, y: source.y};
      const b = {x: target.x + target.width / 2 + 4, y: target.y};
      const bend = Math.max(35, Math.abs(a.x - b.x) * .45);
      ctx.beginPath(); ctx.moveTo(a.x, a.y);
      ctx.bezierCurveTo(a.x - bend, a.y, b.x + bend, b.y, b.x, b.y);
      ctx.stroke();
      ctx.beginPath(); ctx.moveTo(b.x, b.y);
      ctx.lineTo(b.x + length, b.y - length * .5);
      ctx.lineTo(b.x + length, b.y + length * .5);
      ctx.closePath(); ctx.fill(); ctx.restore();
      return;
    }
    const aboveTaste = source.tasteLayer && !withinTaste && target.area !== 'feature' && target.x - target.width / 2 < layerRight
      && target.y + target.height / 2 < source.y - source.height / 2;
    const a = source.tasteLayer && !aboveTaste ? {x: source.x, y: source.y + source.height / 2 + 4}
      : {x: source.x + source.width / 2 + 4, y: source.y};
    const route = linkRoutes.get(link.edgeIndex);
    const tasteInput = tasteInputs.get(link.edgeIndex);
    const b = withinTaste ? {x: target.x + 23 + 15 * layerIndex.get(source.id), y: target.y + target.height / 2 + 4}
      : target.tasteLayer ? tasteInput?.tip ?? {x: target.x, y: target.y - target.height / 2 - 4}
      : route?.points.at(-1) ?? {x: target.x - target.width / 2 - 4, y: target.y};
    ctx.beginPath(); ctx.moveTo(a.x, a.y);
    if (withinTaste) {
      const depth = a.y + 78 + 21 * layerIndex.get(source.id);
      const corner = 18;
      ctx.lineTo(a.x, depth - corner);
      ctx.quadraticCurveTo(a.x, depth, a.x - corner, depth);
      ctx.lineTo(b.x + corner, depth);
      ctx.quadraticCurveTo(b.x, depth, b.x, depth - corner);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      ctx.beginPath(); ctx.moveTo(b.x, b.y);
      ctx.lineTo(b.x - length * .5, b.y + length);
      ctx.lineTo(b.x + length * .5, b.y + length);
      ctx.closePath(); ctx.fill(); ctx.restore();
      return;
    }
    // Leave the layer row below the captions and approach its inputs from above.
    // The short chevrons between its cards show time order only.
    let start = a, end = route?.points.at(-2) ?? b;
    if (aboveTaste) {
      start = {x: a.x + 22, y: source.y - source.height / 2 - 16};
      ctx.bezierCurveTo(a.x + 16, a.y, start.x, start.y + 20, start.x, start.y);
    } else if (source.tasteLayer) {
      start = modelMapTasteExit(source, b.x, tasteLayers);
      ctx.quadraticCurveTo(a.x, start.y, Math.min(a.x + 21, start.x), start.y);
      ctx.lineTo(start.x, start.y);
    }
    if (target.tasteLayer) end = tasteInput?.lane ?? {x: layerLeft - 20, y: b.y - 30 - layerIndex.get(target.id) * 10};
    if (route && !route.direct) {
      const {points} = route;
      for (let index = 1; index < points.length - 1; index++) {
        const before = points[index - 1], point = points[index], after = points[index + 1];
        const back = Math.hypot(before.x - point.x, before.y - point.y);
        const forward = Math.hypot(after.x - point.x, after.y - point.y);
        if (!back || !forward) { ctx.lineTo(point.x, point.y); continue; }
        const radius = Math.min(12, back / 2, forward / 2);
        ctx.lineTo(point.x + (before.x - point.x) * radius / back, point.y + (before.y - point.y) * radius / back);
        ctx.quadraticCurveTo(point.x, point.y,
          point.x + (after.x - point.x) * radius / forward, point.y + (after.y - point.y) * radius / forward);
      }
    } else {
      const bend = (end.x - start.x) * .45;
      const control = target.area === 'feature' ? bend : Math.max(35, bend);
      ctx.bezierCurveTo(start.x + control, start.y, end.x - control, end.y, end.x, end.y);
    }
    if (target.tasteLayer) {
      ctx.lineTo(b.x - 21, end.y);
      ctx.quadraticCurveTo(b.x, end.y, b.x, b.y);
    }
    if (route) ctx.lineTo(b.x, b.y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(b.x, b.y);
    if (target.tasteLayer) {
      ctx.lineTo(b.x - length * .5, b.y - length);
      ctx.lineTo(b.x + length * .5, b.y - length);
    } else {
      ctx.lineTo(b.x - length, b.y - length * .5);
      ctx.lineTo(b.x - length, b.y + length * .5);
    }
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
  function clickAt(event) {
    // The picking canvas can lag the camera when it settles into the opening view.
    const bounds = container.getBoundingClientRect();
    const {x, y} = graph.screen2GraphCoords(event.clientX - bounds.left, event.clientY - bounds.top);
    const node = graph.graphData().nodes.findLast(node => Math.abs(node.x - x) <= node.width / 2
      && Math.abs(node.y - y) <= node.height / 2);
    if (node) events.open(node.id);
    else events.background();
  }
  function positionAreas() {
    const zoom = graph.zoom();
    for (const area of areas) {
      const {element, heading} = areaElements.get(area.title);
      const members = area.members.filter(node => visible.has(node.id));
      element.hidden = !members.length;
      if (element.hidden) continue;
      const left = Math.min(...members.map(node => node.x - node.width / 2));
      const right = Math.max(...members.map(node => node.x + node.width / 2));
      const first = members.reduce((above, node) => node.y < above.y ? node : above);
      const top = first.y - first.height / 2;
      const bottom = Math.max(...members.map(node => node.y + node.height / 2));
      area.x = left - 28; area.y = top - 60;
      area.width = right - left + 56; area.height = bottom - top + 88;
      const point = graph.graph2ScreenCoords(area.x, area.y);
      heading.style.marginLeft = `${first.x - first.width / 2 - area.x}px`;
      element.style.width = `${area.width}px`; element.style.height = `${area.height}px`;
      element.style.transform = `translate(${point.x}px, ${point.y}px) scale(${zoom})`;
    }
  }
  function frameHome({nodes, links}) {
    if (homeFramed || nodes.length !== models.size) return;
    homeFramed = true;
    const width = graph.width(), height = graph.height();
    const horizontalPadding = 16, topPadding = 140, bottomPadding = 100;
    const center = graph.centerAt();
    const atHome = graph.zoom() === home.zoom && Math.hypot(center.x - home.x, center.y - home.y) < 1;
    let best = null;
    for (const {source, target} of links) {
      if (source.area !== 'model' || target.feature_type !== 'interactive') continue;
      const left = Math.min(layerLeft, source.x - source.width / 2);
      const right = Math.max(layerRight, target.x + target.width / 2);
      const cards = [firstLayer, source, target];
      const top = Math.min(...cards.map(card => card.y - card.height / 2));
      const bottom = Math.max(...cards.map(card => card.y + card.height / 2));
      const zoom = Math.min(START_ZOOM, (width - 2 * horizontalPadding) / (right - left),
        (height - topPadding - bottomPadding) / (bottom - top));
      if (zoom < .76) continue;
      const minX = right - (width / 2 - horizontalPadding) / zoom;
      const maxX = left + (width / 2 - horizontalPadding) / zoom;
      const minY = bottom - (height / 2 - bottomPadding) / zoom;
      const maxY = top + (height / 2 - topPadding) / zoom;
      const x = Math.max(minX, Math.min(home.x, maxX)), y = Math.max(minY, Math.min(home.y, maxY));
      const shift = Math.hypot(x - home.x, y - home.y);
      if (!best || zoom > best.zoom || (zoom === best.zoom && shift < best.shift)) best = {x, y, zoom, shift};
    }
    if (best) {
      const {x, y, zoom} = best;
      Object.assign(home, {x, y, zoom});
      if (atHome) graph.centerAt(x, y, 0).zoom(zoom, 0);
    }
  }
  graph.backgroundColor('transparent').nodeId('id').nodeLabel(() => '').linkLabel(() => '')
    .nodeCanvasObject(drawNode).nodeCanvasObjectMode(() => 'replace')
    .nodePointerAreaPaint(paintHit)
    .linkCanvasObject(drawLink).linkCanvasObjectMode(() => 'replace')
    .minZoom(.15).maxZoom(3.5).enableNodeDrag(true)
    .warmupTicks(220).cooldownTicks(90).cooldownTime(2500).d3AlphaDecay(.035).d3VelocityDecay(.5)
    .onNodeHover(node => events.hover(node?.id || null))
    .onNodeClick((_, event) => clickAt(event))
    .onBackgroundClick(clickAt)
    .onNodeDrag(node => {
      if (node.tasteLayer) { node.fx = node.branchX; node.fy = node.branchY; }
      linkRoutes = new Map(); tasteInputs = new Map();
    })
    .onZoomEnd(refreshPointer).onNodeDragEnd(refreshPointer)
    .onRenderFramePre(drawTasteLayers)
    .onRenderFramePost(() => { positionAreas(); events.frame(); })
    .onEngineStop(() => {
      const data = graph.graphData();
      linkRoutes = window.routeModelMapLinks(data.nodes, data.links, tasteLayers);
      tasteInputs = routeModelMapTasteInputs(data.links, tasteLayers);
      frameHome(data);
      refreshPointer(); container.dataset.settled = 'true';
    });
  graph.d3Force('charge', d3.forceManyBody().strength(-450).distanceMax(700));
  graph.d3Force('center', null);
  graph.d3Force('clusters', separation);
  graph.d3Force('direction', direction);
  graph.d3Force('feature-placement', featurePlacement);
  graph.d3Force('depth', d3.forceX(node => node.branchX).strength(node => node.area === 'feature' ? 0 : .055));
  graph.d3Force('branch', d3.forceY(node => node.branchY).strength(node => node.branchStrength ?? (node.area === 'feature' ? 1.1 : .055)));
  graph.d3Force('link').distance(350)
    .strength(link => link.feedback ? 0 : link.target.area === 'feature' ? .002 : outgoing.get(link.source.id).length === 1 ? .2 : .06);
  // Resolve card spacing after the branch and link forces have moved nodes.
  graph.d3Force('collision', collision());

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
  // Start with the four taste horizons in view. Once the layout settles,
  // frameHome fits a connected feature alongside them when space allows.
  home.x = (layerLeft + layerRight) / 2;
  home.y = firstLayer.y;
  home.zoom = Math.max(.15, Math.min(START_ZOOM, (graph.width() - 48) / (layerRight - layerLeft)));
  graph.centerAt(home.x, home.y, 0).zoom(home.zoom, 0);
  return {
    graph,
    update(ids) {
      visible = ids; linkRoutes = new Map(); tasteInputs = new Map();
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
    resetView() { graph.centerAt(home.x, home.y, 350).zoom(home.zoom, 350); },
    center(id) { const node = models.get(id); if (node) graph.centerAt(node.x, node.y, 350); },
    destroy() { resize.disconnect(); if (resizeFrame !== null) cancelAnimationFrame(resizeFrame); graph._destructor(); container.replaceChildren(); },
  };
};
