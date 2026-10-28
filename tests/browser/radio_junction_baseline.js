// Accepted under-control routing from a55675f, before the smoothing changes.
// The user's clarification on #411 explicitly preserves this normal bend.
function acceptedRadioJunction(model, slot, detail = false) {
  const target = model.queueCenters[slot] ?? (slot + .5) * 1000 / model.artworkView.directionCount;
  const x = slot === model.artworkView.connectedSlot ? model.junctionX ?? target : target;
  const fork = slot === model.artworkView.connectedSlot ? model.junctionForkX ?? x : x;
  const {x: cx, y: cy, rx, ry, insetX, insetY} = model.junctionHub;
  const arm = (start, end, depth, shoulder, radiusX, radiusY) => {
    const a = [start, 8], b = [start, shoulder], c = [end, 22], d = [end, depth];
    const points = [[start, -3], a];
    for (let i = 1; i <= 48; i++) {
      const t = i / 48, u = 1 - t;
      points.push(a.map((v, axis) => u ** 3 * v + 3 * u * u * t * b[axis]
        + 3 * u * t * t * c[axis] + t ** 3 * d[axis]));
    }
    return {points, side: Math.sign(start - cx), radiusX, radiusY};
  };
  const trace = (route, reverse = false, move = 'M') => {
    const {side, radiusX, radiusY} = route;
    const points = reverse ? [...route.points].reverse() : route.points;
    const coordinates = p => `${(cx + p[0] * radiusX).toFixed(3)} ${(cy + p[1] * radiusY).toFixed(3)}`;
    let path = '', previous;
    for (const point of points) {
      let q = [(point[0] - cx) / radiusX, (point[1] - cy) / radiusY];
      if (Math.abs(q[1]) < 1.002)
        q[0] = side * Math.max(side * q[0], Math.sqrt(1.002 ** 2 - q[1] ** 2));
      const length = Math.hypot(...q);
      if (length < 1.002) q = length < .00001 ? [0, -1.002] : q.map(v => v * 1.002 / length);
      if (previous) {
        const delta = q.map((v, axis) => v - previous[axis]);
        const norm = delta[0] ** 2 + delta[1] ** 2;
        const t = norm ? Math.max(0, Math.min(1, -(previous[0] * delta[0] + previous[1] * delta[1]) / norm)) : 0;
        if (Math.hypot(previous[0] + t * delta[0], previous[1] + t * delta[1]) < 1.001) {
          const a = Math.atan2(previous[1], previous[0]), b = Math.atan2(q[1], q[0]);
          const turn = Math.atan2(Math.sin(b - a), Math.cos(b - a));
          path += ` L${coordinates([Math.cos(a) * 1.002, Math.sin(a) * 1.002])}` +
            ` A${radiusX * 1.002} ${radiusY * 1.002} 0 0 ${turn > 0 ? 1 : 0}` +
            ` ${coordinates([Math.cos(b) * 1.002, Math.sin(b) * 1.002])}`;
        }
      }
      path += `${previous ? ' L' : move}${coordinates(q)}`;
      previous = q;
    }
    return path;
  };
  if (detail) {
    const depth = Math.max(82, cy + ry - insetY / 2 + 4);
    const left = arm(52, fork - 6, depth, 36, rx - insetX / 2, ry - insetY / 2);
    const right = arm(948, fork + 6, depth, 36, rx - insetX / 2, ry - insetY / 2);
    return trace(left) + ` C${fork - 6} 104 ${x - 6} 104 ${x - 6} 112 ` +
      `C${x - 6} 120 ${x + 6} 120 ${x + 6} 112 ` +
      `C${x + 6} 104 ${fork + 6} 104 ${fork + 6} ${depth} ` + trace(right, true, 'L') + ' Z';
  }
  const outerDepth = Math.max(82, cy + ry + 4), innerDepth = Math.max(68, cy + ry - insetY + 1);
  const left = arm(40, fork - 18, outerDepth, 48, rx, ry);
  const right = arm(960, fork + 18, outerDepth, 48, rx, ry);
  const innerLeft = arm(64, fork, innerDepth, 26, rx - insetX, ry - insetY);
  const innerRight = arm(936, fork, innerDepth, 26, rx - insetX, ry - insetY);
  return trace(left) + ` C${fork - 18} 104 ${x - 18} 104 ${x - 18} 112 L${x + 18} 112 ` +
    `C${x + 18} 104 ${fork + 18} 104 ${fork + 18} ${outerDepth} ` + trace(right, true, 'L') + ' ' +
    trace(innerRight, false, 'L') + ' ' + trace(innerLeft, true, 'L') + ' Z';
}
