export const step = 24;

class Queue {
  items = [];

  push(entry) {
    let i = this.items.length;
    this.items.push(entry);
    while (i > 0) {
      const parent = (i - 1) >> 1;
      if (this.items[parent].priority <= entry.priority) break;
      this.items[i] = this.items[parent];
      i = parent;
    }
    this.items[i] = entry;
  }

  pop() {
    const first = this.items[0], last = this.items.pop();
    if (!this.items.length) return first;
    let i = 0;
    while (2 * i + 1 < this.items.length) {
      let child = 2 * i + 1;
      if (this.items[child + 1]?.priority < this.items[child].priority) child++;
      if (this.items[child].priority >= last.priority) break;
      this.items[i] = this.items[child];
      i = child;
    }
    this.items[i] = last;
    return first;
  }
}

function ports(box, other, selfSide, used) {
  const x = box.x / step, y = box.y / step, w = box.width / step, h = box.height / step;
  const dx = (other.x - box.x) / box.width, dy = (other.y - box.y) / box.height;
  const length = Math.hypot(dx, dy) || 1;
  const facing = [dx, dy, -dx, -dy];
  let best, bestCost = Infinity;
  for (let side = 0; side < 4; side++) {
    if (box === other && side !== selfSide) continue;
    const horizontal = side % 2 === 0;
    const span = horizontal ? h : w;
    const preferred = span / 2 + (horizontal ? dy : dx) / length * (span / 2 - 1);
    for (let slot = 1; slot < span; slot++) {
      const key = `${box.node.id}:${side}:${slot}`;
      const cost = -facing[side] / length * 4 + Math.abs(slot - preferred) / span + (used.get(key) || 0) * 1.05;
      if (cost >= bestCost) continue;
      const px = x + (horizontal ? (side === 0 ? w : 0) : slot);
      const py = y + (horizontal ? slot : (side === 1 ? h : 0));
      const offsetX = horizontal ? 1 - side : 0, offsetY = horizontal ? 0 : 2 - side;
      best = {key, point: [px, py], outside: [px + offsetX, py + offsetY], side};
      bestCost = cost;
    }
  }
  used.set(best.key, (used.get(best.key) || 0) + 1);
  return best;
}

function simplify(points) {
  return points.filter(([x, y], i) => {
    if (!i || i === points.length - 1) return true;
    const [ax, ay] = points[i - 1], [bx, by] = points[i + 1];
    return !(ax === x && x === bx) && !(ay === y && y === by);
  }).map(([x, y]) => [x * step, y * step]);
}

export function routeForeignKeys(boxes, edges, width, height) {
  const cols = width / step + 1, rows = height / step + 1;
  const size = cols * rows;
  const blocked = new Uint8Array(size), traffic = new Uint16Array(size);
  for (let i = 0; i < size; i++) {
    if (i < cols || i >= size - cols || i % cols === 0 || i % cols === cols - 1) blocked[i] = 1;
  }
  for (const box of boxes.values()) {
    const left = box.x / step, top = box.y / step;
    for (let y = top; y <= top + box.height / step; y++) {
      blocked.fill(1, y * cols + left, y * cols + left + box.width / step + 1);
    }
  }

  const seen = new Uint32Array(size), costs = new Int32Array(size), previous = new Int32Array(size);
  const headings = new Uint8Array(size);
  const directions = [1, cols, -1, -cols];
  let generation = 0;

  function connect(start, end) {
    const [sx, sy] = start.outside, [tx, ty] = end.outside;
    const origin = sy * cols + sx, destination = ty * cols + tx;
    const distance = index => Math.abs(tx - index % cols) + Math.abs(ty - Math.floor(index / cols));
    const open = new Queue();
    generation++;
    seen[origin] = generation;
    costs[origin] = 0;
    headings[origin] = start.side;
    open.push({index: origin, cost: 0, priority: 0});
    while (open.items.length) {
      const {index, cost} = open.pop();
      if (cost !== costs[index]) continue;
      if (index === destination) {
        const path = [end.point];
        for (let cell = index; ; cell = previous[cell]) {
          path.push([cell % cols, Math.floor(cell / cols)]);
          traffic[cell]++;
          if (cell === origin) break;
        }
        return simplify(path.concat([start.point]).reverse());
      }
      directions.forEach((offset, direction) => {
        const next = index + offset;
        if (blocked[next]) return;
        const turn = direction === headings[index] ? 0 : 17;
        const nextCost = cost + 8 + turn + Math.min(traffic[next], 5);
        if (seen[next] === generation && costs[next] <= nextCost) return;
        seen[next] = generation;
        costs[next] = nextCost;
        headings[next] = direction;
        previous[next] = index;
        open.push({index: next, cost: nextCost, priority: nextCost + 13 * distance(next)});
      });
    }
    throw new Error('Could not route a foreign key through the space between tables');
  }

  const used = new Map();
  return edges.map(edge => {
    const source = boxes.get(edge.source), target = boxes.get(edge.target);
    const start = ports(source, target || source, 0, used);
    const points = target
      ? connect(start, ports(target, source, 3, used))
      : simplify([start.point, start.outside]);
    return {edge, points, missing: !target};
  });
}
