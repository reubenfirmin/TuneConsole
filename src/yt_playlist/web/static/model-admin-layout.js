/* Dependency ranks guide the forces. Ordering passes reduce crossings before
 * connected branches settle, without pinning nodes into rigid columns. */
window.createModelMapLayout = function(data) {
  const nodes = new Map(data.nodes.map(node => [node.id, {...node, rank: 0}]));
  const incoming = new Map(data.nodes.map(node => [node.id, []]));
  const outgoing = new Map(data.nodes.map(node => [node.id, []]));
  for (const edge of data.edges) {
    incoming.get(edge.target).push(nodes.get(edge.source));
    outgoing.get(edge.source).push(nodes.get(edge.target));
  }
  const remaining = new Map([...incoming].map(([id, values]) => [id, values.length]));
  const ordered = [...nodes.values()].filter(node => !remaining.get(node.id));
  for (let cursor = 0; cursor < ordered.length; cursor++) {
    const node = ordered[cursor];
    for (const target of outgoing.get(node.id)) {
      target.rank = Math.max(target.rank, node.rank + 1);
      remaining.set(target.id, remaining.get(target.id) - 1);
      if (!remaining.get(target.id)) ordered.push(target);
    }
  }
  if (ordered.length !== nodes.size) throw new Error('Model inventory has cyclic dependencies');

  // Keep short branches near their consumer instead of forcing every source
  // into one crowded first rank. Every edge still advances at least one rank.
  for (const node of [...ordered].reverse()) {
    const targets = outgoing.get(node.id);
    if (targets.length) node.rank = Math.min(...targets.map(target => target.rank)) - 1;
  }
  const models = ordered.filter(node => node.area === 'model');
  const features = [...nodes.values()].filter(node => node.area === 'feature');
  const first = Math.min(...models.map(node => node.rank));
  const last = Math.max(...models.map(node => node.rank));
  // Siblings with the same inputs and consumers form one force cluster. This
  // groups provider heads and audio specialists by actual dependencies.
  const siblings = new Map();
  for (const node of ordered) {
    node.width = node.area === 'feature' ? 212 : 188;
    node.height = node.area === 'feature' ? 68 : 64;
    if (node.area === 'feature') continue;
    const key = JSON.stringify([node.group, node.kind,
      incoming.get(node.id).map(value => value.id).sort(), outgoing.get(node.id).map(value => value.id).sort()]);
    if (!siblings.has(key)) siblings.set(key, []);
    siblings.get(key).push(node);
  }
  const clusters = [...siblings.values()].flatMap(members => members.length >= 3 ? [{members}] : members.map(node => ({members: [node]})));
  const clusterOf = new Map();
  for (const cluster of clusters) {
    cluster.rank = cluster.members[0].rank;
    cluster.group = cluster.members[0].group;
    cluster.members.forEach((node, index) => {
      clusterOf.set(node.id, cluster);
      const radius = cluster.members.length > 1 ? Math.sqrt(index + .5) : 0;
      node.offsetX = Math.cos(index * 2.399963) * radius * 110;
      node.offsetY = Math.sin(index * 2.399963) * radius * 85;
    });
    const xs = cluster.members.map(node => node.offsetX), ys = cluster.members.map(node => node.offsetY);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    for (const node of cluster.members) { node.offsetX -= cx; node.offsetY -= cy; }
    cluster.width = Math.max(...xs) - Math.min(...xs) + cluster.members[0].width;
    cluster.height = Math.max(...ys) - Math.min(...ys) + cluster.members[0].height;
  }
  const levels = Array.from({length: last - first + 1}, () => []);
  const groups = new Map(data.groups.map((group, index) => [group, index]));
  for (const cluster of clusters) levels[cluster.rank - first].push(cluster);
  for (const level of levels) level.sort((a, b) => (groups.get(a.group) ?? groups.size) - (groups.get(b.group) ?? groups.size));
  function positions() {
    for (const level of levels) level.forEach((cluster, index) => { cluster.order = (index + .5) / level.length; });
  }
  positions();
  const average = (neighbors, cluster) => {
    const values = cluster.members.flatMap(node => neighbors.get(node.id)).filter(node => clusterOf.has(node.id));
    return values.length ? values.reduce((sum, value) => sum + clusterOf.get(value.id).order, 0) / values.length : cluster.order;
  };
  for (let pass = 0; pass < 8; pass++) {
    const forward = pass % 2 === 0, neighbors = forward ? incoming : outgoing;
    for (const level of forward ? levels : [...levels].reverse()) {
      level.sort((a, b) => average(neighbors, a) - average(neighbors, b));
      positions();
    }
  }
  const widths = levels.map(level => Math.max(0, ...level.map(cluster => cluster.width)));
  const columns = [0];
  for (let index = 1; index < levels.length; index++) columns.push(columns[index - 1] + (widths[index - 1] + widths[index]) / 2 + 160);
  for (const [rank, level] of levels.entries()) {
    const height = level.reduce((sum, cluster) => sum + cluster.height + 100, -100);
    let top = -height / 2;
    for (const cluster of level) {
      const x = columns[rank] - columns.at(-1) / 2, y = top + cluster.height / 2;
      for (const node of cluster.members) {
        node.x = node.branchX = x + node.offsetX;
        node.y = node.branchY = y + node.offsetY;
      }
      top += cluster.height + 100;
    }
  }
  // Place the consumer clouds alongside the ends of the model branches. A
  // distant terminal model must not reserve an empty band across every area;
  // dependency direction and collision keep each actual connection clear.
  const featureLeft = Math.max(...models.map(node => node.x + node.width / 2)) - 120;
  const areas = data.feature_areas.map(title => ({title, members: features.filter(node => node.app_area === title)}))
    .filter(area => area.members.length);
  for (const [index, area] of areas.entries()) {
    area.members.forEach((node, position) => {
      const radius = area.members.length > 1 ? Math.sqrt(position + .5) : 0;
      const angle = position * 2.399963 + index * .73 + .4;
      node.offsetX = Math.cos(angle) * radius * 120;
      node.offsetY = Math.sin(angle) * radius * 94;
    });
    const xs = area.members.map(node => node.offsetX), ys = area.members.map(node => node.offsetY);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    for (const node of area.members) { node.offsetX -= cx; node.offsetY -= cy; }
    area.width = Math.max(...xs) - Math.min(...xs) + 268;
    area.height = Math.max(...ys) - Math.min(...ys) + 156;
  }
  let top = Math.min(...models.map(node => node.y - node.height / 2)) - 40;
  for (const [index, area] of areas.entries()) {
    const x = featureLeft + 320 + Math.sin(index * 1.7 + .4) * 95;
    const y = top + area.height / 2 + 16;
    area.x = x - area.width / 2; area.y = top;
    area.members.forEach(node => {
      node.x = node.branchX = x + node.offsetX;
      node.y = node.branchY = y + node.offsetY;
    });
    clusters.push(area);
    top += area.height + 100;
  }
  let visible = [];
  const featureBoundary = () => {
    for (const node of visible) {
      if (node.fx != null || node.area !== 'feature') continue;
      const overlap = featureLeft - (node.x + node.vx - node.width / 2);
      if (overlap > 0) node.vx += overlap;
    }
  };
  featureBoundary.initialize = values => { visible = values; };
  // Keep dependency order even as links, repulsion and dragging move branches.
  // Only active edges participate when search/focus filters hide other nodes.
  const links = data.edges.map(edge => [nodes.get(edge.source), nodes.get(edge.target)]);
  let active = new Set();
  const direction = () => {
    for (const [source, target] of links) {
      if (!active.has(source) || !active.has(target)) continue;
      const gap = (source.width + target.width) / 2 + 100;
      const overlap = gap - (target.x + target.vx - source.x - source.vx);
      if (overlap <= 0) continue;
      const push = overlap * .5;
      if (source.fx == null) source.vx -= push;
      if (target.fx == null) target.vx += push;
    }
  };
  direction.initialize = values => { active = new Set(values); };
  // Cohesion keeps sibling clouds compact, while a separate exclusion area
  // gives each cloud breathing room from other branches and individual nodes.
  const separation = alpha => {
    const boxes = clusters.map(cluster => {
      const members = cluster.members.filter(node => active.has(node));
      if (!members.length) return null;
      const left = Math.min(...members.map(node => node.x - node.width / 2));
      const right = Math.max(...members.map(node => node.x + node.width / 2));
      const top = Math.min(...members.map(node => node.y - node.height / 2));
      const bottom = Math.max(...members.map(node => node.y + node.height / 2));
      const x = (left + right) / 2, y = (top + bottom) / 2;
      const offsetX = (Math.min(...members.map(node => node.offsetX)) + Math.max(...members.map(node => node.offsetX))) / 2;
      const offsetY = (Math.min(...members.map(node => node.offsetY)) + Math.max(...members.map(node => node.offsetY))) / 2;
      if (members.length > 1) for (const node of members) {
        node.vx += (x + node.offsetX - offsetX - node.x) * .16 * alpha;
        node.vy += (y + node.offsetY - offsetY - node.y) * .16 * alpha;
      }
      return {members, x, y, width: right - left, height: bottom - top};
    }).filter(Boolean);
    for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i], b = boxes[j];
      if (a.members.length === 1 && b.members.length === 1) continue;
      const dx = b.x - a.x, dy = b.y - a.y;
      const ox = (a.width + b.width) / 2 + 130 - Math.abs(dx);
      const oy = (a.height + b.height) / 2 + 110 - Math.abs(dy);
      if (ox <= 0 || oy <= 0) continue;
      const axis = ox < oy ? 'x' : 'y', sign = (axis === 'x' ? dx : dy) >= 0 ? -1 : 1;
      const push = Math.min(35, (axis === 'x' ? ox : oy) * .12) * sign;
      for (const node of a.members) if (node['f' + axis] == null) node['v' + axis] += push;
      for (const node of b.members) if (node['f' + axis] == null) node['v' + axis] -= push;
    }
  };
  separation.initialize = values => { active = new Set(values); };
  return {nodes, outgoing, direction, separation, featureBoundary, areas};
};
