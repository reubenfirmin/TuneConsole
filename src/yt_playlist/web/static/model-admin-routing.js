/* Route feature entrances around neighboring cards after the force layout
 * settles. One visibility graph per consumer is reused by all its input links. */
window.routeModelMapFeatures = function(nodes) {
  const features = nodes.filter(node => node.area === 'feature'), routes = new Map();
  if (!features.length) return routes;
  const boxes = features.map(node => ({id: node.id,
    left: node.x - node.width / 2 - 16, right: node.x + node.width / 2 + 16,
    top: node.y - node.height / 2 - 16, bottom: node.y + node.height / 2 + 16}));
  const entranceX = Math.min(...boxes.map(box => box.left)) - 64;
  function crosses(a, b, box) {
    let near = 0, far = 1;
    for (const [axis, low, high] of [['x', box.left + .01, box.right - .01], ['y', box.top + .01, box.bottom - .01]]) {
      const delta = b[axis] - a[axis];
      if (!delta) { if (a[axis] <= low || a[axis] >= high) return false; }
      else {
        const t0 = (low - a[axis]) / delta, t1 = (high - a[axis]) / delta;
        near = Math.max(near, Math.min(t0, t1)); far = Math.min(far, Math.max(t0, t1));
        if (near >= far) return false;
      }
    }
    return near < far;
  }
  for (const node of features) {
    const end = {x: node.x - node.width / 2 - 24, y: node.y};
    const start = {x: entranceX, y: node.y};
    const obstacles = boxes.filter(box => box.id !== node.id && box.left < end.x);
    const points = [start, end, ...obstacles.flatMap(box => [
      {x: box.left, y: box.top}, {x: box.left, y: box.bottom},
      {x: box.right, y: box.top}, {x: box.right, y: box.bottom},
    ]).filter(point => point.x < end.x)];
    const costs = points.map(() => Infinity), previous = points.map(() => -1), visited = new Set();
    costs[0] = 0;
    // The inventory has 21 consumers. This bounded dense Dijkstra pass runs
    // once when geometry changes, never for each edge or animation frame.
    while (visited.size < points.length) {
      let current = -1;
      for (let index = 0; index < points.length; index++) {
        if (!visited.has(index) && (current < 0 || costs[index] < costs[current])) current = index;
      }
      if (current < 0 || !Number.isFinite(costs[current])) break;
      visited.add(current);
      if (current === 1) break;
      for (let next = 0; next < points.length; next++) {
        if (visited.has(next) || obstacles.some(box => crosses(points[current], points[next], box))) continue;
        const cost = costs[current] + Math.hypot(points[next].x - points[current].x, points[next].y - points[current].y);
        if (cost < costs[next]) { costs[next] = cost; previous[next] = current; }
      }
    }
    if (!visited.has(1)) continue;
    const path = [];
    for (let index = 1; index >= 0; index = previous[index]) path.push(points[index]);
    path.reverse(); path.push({x: end.x + 20, y: end.y});
    routes.set(node.id, path);
  }
  return routes;
};
