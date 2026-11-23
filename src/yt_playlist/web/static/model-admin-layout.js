/* Consumer rows guide vertical ordering. Dependency ranks guide horizontal flow. */
const modelMapFeatureOrder = [
  'revisit', 'tv', 'mixtapes', 'on-air', 'vibe-mixes', 'vibe-feedback', 'stories',
  'track-discovery', 'artist-discovery', 'road-trip', 'clusters', 'similar-songs',
  'albums', 'album-notes',
];

// Pack neighboring cards around their desired centers. When a run of cards
// needs more space, share the displacement across the run.
function spaceModelMapRank(level) {
  const spacing = 60;
  const blocks = [];
  for (const cluster of level) {
    const height = cluster.height + spacing;
    const weight = cluster.anchor == null ? 1 : 3;
    let block = {members: [cluster], height, weight, top: cluster.desiredY - height / 2};
    while (blocks.length && blocks.at(-1).top + blocks.at(-1).height > block.top) {
      const prior = blocks.pop();
      const totalWeight = prior.weight + block.weight;
      const top = (prior.top * prior.weight + (block.top - prior.height) * block.weight) / totalWeight;
      block = {members: prior.members.concat(block.members), height: prior.height + block.height, weight: totalWeight, top};
    }
    blocks.push(block);
  }
  for (const block of blocks) {
    let top = block.top;
    for (const cluster of block.members) {
      const height = cluster.height + spacing;
      cluster.y = top + height / 2;
      top += height;
    }
  }
}

window.createModelMapLayout = function(data) {
  const nodes = new Map(data.nodes.map(node => [node.id, {...node, rank: 0}]));
  const tasteLayers = data.periods.flatMap(({id}) => nodes.has(id) ? [nodes.get(id)] : []);
  const layerNodes = new Set(tasteLayers);
  const rankOwner = node => layerNodes.has(node) ? tasteLayers[0] : node;
  const rankNodes = [...new Set([...nodes.values()].map(rankOwner))];
  const incoming = new Map(data.nodes.map(node => [node.id, []]));
  const outgoing = new Map(data.nodes.map(node => [node.id, []]));
  const rankIncoming = new Map(rankNodes.map(node => [node, 0]));
  const rankOutgoing = new Map(rankNodes.map(node => [node, new Set()]));
  // Serving precedes feedback from the next interaction. Rank the serving dependencies.
  const dependencies = data.edges.filter(edge => !edge.feedback);
  for (const edge of dependencies) {
    const source = nodes.get(edge.source), target = nodes.get(edge.target);
    incoming.get(target.id).push(source);
    outgoing.get(source.id).push(target);
    const from = rankOwner(source), to = rankOwner(target), targets = rankOutgoing.get(from);
    if (from === to || targets.has(to)) continue;
    targets.add(to);
    rankIncoming.set(to, rankIncoming.get(to) + 1);
  }
  // Rank the four horizons as one block. Their visual order needs no invented edges.
  const rankOrder = rankNodes.filter(node => !rankIncoming.get(node));
  for (let cursor = 0; cursor < rankOrder.length; cursor++) {
    const node = rankOrder[cursor];
    for (const target of rankOutgoing.get(node)) {
      target.rank = Math.max(target.rank, node.rank + 1);
      rankIncoming.set(target, rankIncoming.get(target) - 1);
      if (!rankIncoming.get(target)) rankOrder.push(target);
    }
  }
  if (rankOrder.length !== rankNodes.length) throw new Error('Model inventory has cyclic dependencies');

  // Keep short branches near their consumer instead of forcing every source
  // into one crowded first rank. Every edge still advances at least one rank.
  const features = [...nodes.values()].filter(node => node.area === 'feature');
  for (const node of features) node.rank = Math.max(node.rank, (tasteLayers[0]?.rank ?? 0) + 1);
  for (const node of [...rankOrder].reverse()) {
    const targets = rankOutgoing.get(node);
    if (targets.size) node.rank = Math.min(...[...targets].map(target => target.rank)) - 1;
  }
  for (const node of tasteLayers) node.rank = tasteLayers[0].rank;
  const ordered = rankOrder.flatMap(node => layerNodes.has(node) ? tasteLayers : [node]);
  const models = ordered.filter(node => node.area === 'model');
  const first = Math.min(...models.map(node => node.rank));
  const last = Math.max(...models.map(node => node.rank));

  const areas = data.feature_areas.map(title => ({title, members: features.filter(node => node.app_area === title)}))
    .filter(area => area.members.length);
  const featureOrder = new Map(modelMapFeatureOrder.map((id, index) => [id, index]));
  const rows = new Map();
  let row = -690;
  for (const area of areas) {
    area.members.sort((a, b) => (featureOrder.get(a.id) ?? Infinity) - (featureOrder.get(b.id) ?? Infinity));
    for (const node of area.members) {
      // Leave vertical room on both sides of Vibe for the routes from taste.
      if (node.id === 'vibe-mixes') row += 96;
      if (node.id === 'road-trip') row += 58;
      rows.set(node.id, row);
      row += 112;
      if (node.id === 'vibe-mixes') row += 230;
    }
    row += area.members.some(node => node.id === 'tv') ? 130 : 58;
  }
  // Dedicated producers follow their feature. Shared inputs sit between the
  // branches they serve and can still pull on one another during settling.
  const anchors = new Map();
  const precursors = [];
  // Keep shared preparation beside the feature that serves its main output.
  const preferredConsumer = new Map([
    ['mix-tape-preparation', 'mixtapes'],
    ['prepared-album-candidates', 'albums'],
  ]);
  function followFeature(target, y) {
    const producers = incoming.get(target.id).filter(node =>
      node.area === 'model' && !layerNodes.has(node)
      && (outgoing.get(node.id).length === 1 || preferredConsumer.get(node.id) === target.id));
    producers.forEach((node, index) => {
      const producerY = y + (index - (producers.length - 1) / 2) * 130;
      anchors.set(node.id, producerY);
      precursors.push([node, target]);
      followFeature(node, producerY);
    });
  }
  for (const node of features) followFeature(node, rows.get(node.id));
  const anchor = (id, feature, offset = 0) => anchors.set(id, rows.get(feature) + offset);
  anchor('tv-provider-evidence', 'tv', -340);
  anchor('video-motion', 'tv', -200);
  // Keep the album ranking chain below the Similar songs inputs.
  anchor('album-ranker', 'albums', 64);
  anchor('cluster-expansion', 'clusters', -24);
  anchor('cluster-connections', 'clusters', 106);
  anchor('ranked-library-pools', 'mixtapes', 64);
  anchor('ppr', 'on-air', -100);
  anchor('group-choice', 'on-air', 160);
  anchor('prepared-fresh-pool', 'on-air', 85);
  // Keep sound evidence above listening history and catalog evidence below it.
  anchor('musicnn', 'vibe-mixes', -1090);
  anchor('audio-excerpts', 'vibe-mixes', -1190);
  anchor('joint-vibe', 'vibe-mixes', -800);
  anchor('vibe-intensity', 'vibe-mixes', -565);
  anchor('musical-profile', 'vibe-mixes', -760);
  anchor('listening-evidence', 'vibe-mixes', -60);
  anchor('choice-response', 'vibe-mixes', 80);
  anchor('choices', 'vibe-mixes', 130);
  anchor('groups', 'vibe-mixes', -190);
  anchor('provider-radio', 'vibe-mixes', 360);
  anchor('discovery-intake', 'vibe-mixes', 360);
  anchor('discovered-track-pool', 'vibe-mixes', 360);
  anchor('taxonomy', 'vibe-mixes', 550);
  anchor('metadata', 'vibe-mixes', 680);
  anchor('ab-mood_party', 'vibe-mixes', -140);
  anchor('ab-danceability', 'vibe-mixes', -390);
  anchor('ab-mood_acoustic', 'vibe-mixes', -260);
  anchor('acousticbrainz-match', 'vibe-mixes', -350);
  anchor('prepared-vibe-fresh-pool', 'vibe-mixes', -230);
  anchor('track-svd', 'vibe-mixes', -690);
  anchor('item2vec', 'vibe-mixes', -550);
  anchor('daily-selection', 'vibe-mixes', -760);
  anchor('relationship-baskets', 'vibe-mixes', -640);
  anchor('track-cooccurrence', 'vibe-mixes', -585);
  anchor('content', 'vibe-mixes', -425);
  anchor('contexts', 'vibe-mixes', 400);
  anchor('fresh-ranker', 'vibe-mixes', 415);
  anchor('personalized-track-scoring', 'vibe-mixes', 470);
  anchor('prepared-mix-listening', 'vibe-mixes', 290);
  anchor('library-taste-summary', 'mixtapes', 110);
  anchor('cold-projection', 'vibe-mixes', 550);
  anchor('curated-collections', 'vibe-mixes', 770);
  anchor('playlist-sequencing', 'track-discovery', 44);
  anchor('related-artists', 'artist-discovery');
  anchor('artist-content', 'artist-discovery', -115);
  anchor('artist-svd', 'artist-discovery');
  anchor('lastfm', 'artist-discovery', 115);
  anchor('ytm-related-artists', 'artist-discovery', 230);
  anchor('discovered-albums', 'track-discovery', 90);

  // Siblings with the same inputs and consumers form one force cluster. This
  // groups provider heads and audio specialists by actual dependencies.
  const siblings = new Map();
  for (const node of ordered) {
    node.tasteLayer = layerNodes.has(node);
    node.width = node.area === 'feature' ? 212 : node.tasteLayer ? 166 : 188;
    node.height = node.area === 'feature' ? 68 : 64;
    if (node.area === 'feature' || node.tasteLayer) continue;
    const key = JSON.stringify([node.group, node.kind,
      incoming.get(node.id).map(value => value.id).sort(), outgoing.get(node.id).map(value => value.id).sort()]);
    if (!siblings.has(key)) siblings.set(key, []);
    siblings.get(key).push(node);
  }
  const clusters = [...siblings.values()].flatMap(members => members.length >= 3 ? [{members}] : members.map(node => ({members: [node]})));
  if (tasteLayers.length) clusters.push({members: tasteLayers, taste: true});
  const clusterOf = new Map();
  for (const cluster of clusters) {
    cluster.rank = cluster.members[0].rank;
    cluster.group = cluster.members[0].group;
    // Identical spokes read in one column. A spiral can put a sibling in
    // front of its neighbor's arrow even when their cards have enough room.
    cluster.members.forEach((node, index) => {
      clusterOf.set(node.id, cluster);
      node.offsetX = cluster.taste ? index * (node.width + 46) : 0;
      node.offsetY = cluster.taste ? 0 : index * 114;
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
  for (const level of levels) {
    level.sort((a, b) => (groups.get(a.group) ?? groups.size) - (groups.get(b.group) ?? groups.size));
    level.forEach((cluster, index) => {
      cluster.anchor = cluster.taste ? 0 : anchors.get(cluster.members[0].id);
      cluster.y = cluster.anchor ?? (index - (level.length - 1) / 2) * 136;
    });
  }
  const average = (neighbors, cluster) => {
    const adjacent = new Set(cluster.members.flatMap(node => neighbors.get(node.id)).map(node => clusterOf.get(node.id) ?? node));
    adjacent.delete(cluster);
    let total = 0, weight = 0;
    for (const neighbor of adjacent) {
      const influence = 1 / Math.sqrt(Math.max(1, Math.abs(neighbor.rank - cluster.rank)));
      total += (neighbor.y ?? rows.get(neighbor.id)) * influence;
      weight += influence;
    }
    return weight ? total / weight : cluster.y;
  };
  for (let pass = 0; pass < 8; pass++) {
    const forward = pass % 2 === 0, neighbors = forward ? incoming : outgoing;
    for (const level of forward ? levels : [...levels].reverse()) {
      for (const cluster of level) {
        const adjacentY = average(neighbors, cluster);
        cluster.desiredY = cluster.anchor ?? (cluster.y + adjacentY) / 2;
      }
      level.sort((a, b) => a.desiredY - b.desiredY);
      spaceModelMapRank(level);
    }
  }
  const widths = levels.map(level => Math.max(0, ...level.map(cluster => cluster.width)));
  const columns = [0];
  for (let index = 1; index < levels.length; index++) {
    const gap = levels[index].some(cluster => cluster.taste) ? 360 : 160;
    columns.push(columns[index - 1] + (widths[index - 1] + widths[index]) / 2 + gap);
  }
  for (const [rank, level] of levels.entries()) {
    for (const cluster of level) {
      const x = columns[rank] - columns.at(-1) / 2;
      for (const node of cluster.members) {
        node.x = node.branchX = x + node.offsetX;
        node.y = node.branchY = cluster.y + node.offsetY;
        if (cluster.anchor != null) node.branchStrength = .65;
        if (node.tasteLayer) {
          node.fx = node.x;
          node.fy = node.y;
        }
      }
    }
  }
  // Give each feature its own depth. A long preparation path such as TV
  // doesn't reserve horizontal space in every other feature branch.
  const featureLeft = Math.max(...tasteLayers.map(node => node.x + node.width / 2));
  function minimumFeatureLeft(node) {
    // Revisit uses history and metadata before the taste row. Leave an open
    // lane beside the row's exit for links that continue down to Stories.
    if (node.id === 'revisit') return -Infinity;
    if (node.id === 'vibe-feedback') return featureLeft + 88;
    // Leave a lane beside the album cards for connections from the taste row.
    if (node.id === 'taste-page') return featureLeft + 510;
    return featureLeft + (node.id === 'stories' ? 64 : 0);
  }
  const rightEdge = node => node ? node.x + node.width / 2 : -Infinity;
  const rightmost = feeders => feeders.reduce((right, source) => rightEdge(source) > rightEdge(right) ? source : right, null);
  const featureX = (node, feeder) => Math.max(minimumFeatureLeft(node) + 24, rightEdge(feeder) + 116) + node.width / 2;
  const precursorX = (node, target) => target.x - (node.width + target.width) / 2 - 106;
  for (const node of features) {
    node.y = node.branchY = rows.get(node.id);
    node.x = node.branchX = featureX(node, rightmost(incoming.get(node.id)));
  }
  for (const [node, target] of precursors) {
    node.x = node.branchX = Math.max(node.x, precursorX(node, target));
    node.branchY = anchors.get(node.id);
    node.branchStrength = 1.1;
  }
  let active = new Set();
  const updateActive = values => { active = new Set(values); };
  const featurePlacement = alpha => {
    // Close the gap from both ends so a long upstream link can't pull a
    // dedicated precursor far away from the branch it serves.
    for (const [node, target] of precursors) {
      if (!active.has(node) || !active.has(target)) continue;
      const gap = precursorX(node, target) - node.x - node.vx;
      if (node.fx == null) node.vx += gap * 2 * alpha;
      if (target.fx == null) target.vx -= gap * .55 * alpha;
    }
    for (const node of features) {
      if (!active.has(node) || node.fx != null) continue;
      const feeders = incoming.get(node.id).filter(source => active.has(source));
      const feeder = rightmost(feeders);
      // Leave room to enter when the last producer is in a distant row.
      const shared = feeders.length > 1 && Math.abs(feeder.y - node.y) > node.height * 4;
      const x = feeder && !shared ? featureX(node, feeder) : node.branchX;
      node.vx += (x - node.x - node.vx) * .28 * alpha;
      const overlap = minimumFeatureLeft(node) - (node.x + node.vx - node.width / 2);
      if (overlap > 0) node.vx += overlap;
    }
  };
  featurePlacement.initialize = updateActive;
  // Keep dependency order even as links, repulsion and dragging move branches.
  // Only active edges participate when search/focus filters hide other nodes.
  const links = dependencies.map(edge => [nodes.get(edge.source), nodes.get(edge.target)]);
  const direction = () => {
    for (const [source, target] of links) {
      if (!active.has(source) || !active.has(target)) continue;
      if (source.tasteLayer && target.tasteLayer) continue;
      const gap = (source.width + target.width) / 2 + (target.tasteLayer ? 250 : 100);
      const overlap = gap - (target.x + target.vx - source.x - source.vx);
      if (overlap <= 0) continue;
      const push = overlap * .5;
      if (source.fx == null) source.vx -= push;
      if (target.fx == null) target.vx += push;
    }
  };
  direction.initialize = updateActive;
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
  separation.initialize = updateActive;
  return {nodes, outgoing, direction, separation, featurePlacement, areas, tasteLayers};
};
