/* Keep serving links moving forward and clear of the cards along the way. */
function modelMapTasteExit(source, targetX, layers) {
  const last = layers.at(-1), depth = layers.length - 1 - layers.indexOf(source);
  return {
    x: Math.min(last.x + last.width / 2 + 24, targetX - 24),
    y: source.y + source.height / 2 + 34 + depth * 10,
  };
}

function routeModelMapTasteInputs(links, layers) {
  const inputs = links.filter(({source, target, feedback}) => !feedback && !source.tasteLayer && target.tasteLayer);
  const sources = [...new Set(inputs.map(link => link.source))].sort((a, b) => a.y - b.y);
  const first = layers[0];
  const left = first.x - first.width / 2, top = first.y - first.height / 2;
  const lanes = new Map(sources.map((source, index) => [source, {
    x: left - 34, y: top - 30 - (sources.length - index - 1) * 24,
  }]));
  const routes = new Map();
  for (const target of layers) {
    const feeders = inputs.filter(link => link.target === target)
      .sort((a, b) => lanes.get(b.source).y - lanes.get(a.source).y);
    // Each source takes one track above the row. Sources farther up turn
    // farther to the right when several of them enter the same horizon.
    const spacing = Math.min(15, (target.width - 30) / Math.max(1, feeders.length - 1));
    feeders.forEach(({source, edgeIndex}, index) => routes.set(edgeIndex, {
      lane: lanes.get(source),
      tip: {x: target.x + (index - (feeders.length - 1) / 2) * spacing, y: top - 4},
    }));
  }
  return routes;
}

window.routeModelMapLinks = function(nodes, links, tasteLayers) {
  const routes = new Map();
  const boxes = nodes.map(node => ({node,
    left: node.x - node.width / 2 - 16, right: node.x + node.width / 2 + 16,
    top: node.y - node.height / 2 - 16, bottom: node.y + node.height / 2 + 16}));
  const startFor = (source, targetX) => source.tasteLayer ? modelMapTasteExit(source, targetX, tasteLayers)
    : {x: source.x + source.width / 2 + 4, y: source.y};
  // Order the ports by approach so that short links from a nearby card
  // can enter above or below the flatter links from distant sources.
  const portOffsets = new Map();
  for (const id of ['vibe-mixes', 'taste-page']) {
    const inputs = links.filter(link => !link.feedback && link.target.id === id)
      .map(({source, target, edgeIndex}) => {
        const targetX = target.x - target.width / 2 - 4;
        const start = startFor(source, targetX);
        return {edgeIndex, approach: (start.y - target.y) / Math.max(1, targetX - start.x)};
      }).sort((a, b) => a.approach - b.approach);
    const steps = inputs.length - 1;
    const spacing = Math.min(14, 48 / Math.max(1, steps));
    inputs.forEach(({edgeIndex}, index) => portOffsets.set(edgeIndex, spacing * (index - steps / 2)));
  }
  function crosses(a, b, box) {
    const left = box.left + .01, right = box.right - .01;
    const top = box.top + .01, bottom = box.bottom - .01;
    if ((a.x <= left && b.x <= left) || (a.x >= right && b.x >= right)
      || (a.y <= top && b.y <= top) || (a.y >= bottom && b.y >= bottom)) return false;
    const dx = b.x - a.x, dy = b.y - a.y;
    if (!dx || !dy) return true;
    // The segment crosses if its intervals inside both axes overlap.
    const enterX = ((dx > 0 ? left : right) - a.x) / dx;
    const exitX = ((dx > 0 ? right : left) - a.x) / dx;
    const enterY = ((dy > 0 ? top : bottom) - a.y) / dy;
    const exitY = ((dy > 0 ? bottom : top) - a.y) / dy;
    return Math.max(0, enterX, enterY) < Math.min(1, exitX, exitY);
  }
  function forwardRoute(start, end, tip, obstacles, feature) {
    if (start.x > end.x || obstacles.some(box => crosses(end, tip, box))) return;
    const top = Math.min(start.y, end.y), bottom = Math.max(start.y, end.y);
    const near = obstacles.filter(box => box.bottom > top && box.top < bottom);
    const bend = Math.max(feature ? 0 : 35, (end.x - start.x) * .45);
    // Check the curve itself before finding a detour. The card padding also
    // leaves space for smoothing the corners of a detour in the renderer.
    let prior = start, clear = true;
    for (let step = 1; near.length && clear && step <= 20; step++) {
      const t = step / 20, u = 1 - t;
      const middle = 3 * t * u;
      const point = {
        x: u ** 3 * start.x + middle * (u * (start.x + bend) + t * (end.x - bend)) + t ** 3 * end.x,
        y: u ** 2 * (1 + 2 * t) * start.y + t ** 2 * (1 + 2 * u) * end.y,
      };
      clear = !near.some(box => crosses(prior, point, box));
      prior = point;
    }
    if (clear) return {points: [start, end, tip], direct: true};
    const points = [start, end, ...obstacles.flatMap(box => {
      const left = Math.max(start.x, box.left), right = Math.min(end.x, box.right);
      if (right < left) return [];
      return [{x: left, y: box.top}, {x: left, y: box.bottom},
        {x: right, y: box.top}, {x: right, y: box.bottom}];
    })];
    const costs = points.map(() => Infinity), previous = points.map(() => -1), visited = new Set();
    const remaining = points.map(point => Math.hypot(end.x - point.x, end.y - point.y));
    costs[0] = 0;
    // The visibility graph contains only card corners between the ports.
    // It is rebuilt when the layout settles, outside the animation loop.
    while (visited.size < points.length) {
      let current = -1;
      for (let index = 0; index < points.length; index++) {
        if (!visited.has(index) && (current < 0 || costs[index] + remaining[index] < costs[current] + remaining[current])) current = index;
      }
      if (current < 0 || !Number.isFinite(costs[current])) break;
      visited.add(current);
      if (current === 1) break;
      for (let next = 0; next < points.length; next++) {
        if (visited.has(next) || points[next].x < points[current].x
          || obstacles.some(box => crosses(points[current], points[next], box))) continue;
        const cost = costs[current] + Math.hypot(points[next].x - points[current].x, points[next].y - points[current].y);
        if (cost < costs[next]) { costs[next] = cost; previous[next] = current; }
      }
    }
    if (!visited.has(1)) return;
    const path = [];
    for (let index = 1; index >= 0; index = previous[index]) path.push(points[index]);
    path.reverse(); path.push(tip);
    return {points: path, direct: false};
  }
  for (const {source, target, feedback, edgeIndex} of links) {
    const feature = target.area === 'feature';
    if (feedback || target.tasteLayer || (source.tasteLayer && !feature)) continue;
    const offset = portOffsets.get(edgeIndex) ?? 0;
    const radius = target.height / 2;
    const inset = radius - Math.sqrt(radius * radius - offset * offset);
    const tip = {x: target.x - target.width / 2 + inset - 4, y: target.y + offset};
    const end = {x: tip.x - 20, y: tip.y};
    const start = startFor(source, tip.x);
    const obstacles = boxes.filter(box => box.node !== source && box.node !== target && box.right > start.x && box.left < tip.x);
    const route = forwardRoute(start, end, tip, obstacles, feature)
      ?? (feature ? forwardRoute(start, end, tip, obstacles.filter(box => box.node.area === 'feature'), true) : undefined);
    if (route) routes.set(edgeIndex, route);
  }
  return routes;
};
