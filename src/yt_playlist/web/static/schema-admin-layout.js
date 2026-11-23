import {routeForeignKeys, step} from './schema-admin-routing.js';

const width = 13 * step, height = 5 * step;
const margin = 4 * step;
const goldenAngle = Math.PI * (3 - Math.sqrt(5));

// Keep the catalog in the middle, with account and source data to the right,
// listening and audio below, and the recommendation pipeline to the left.
// The remaining positions are derived from the installed relations.
const landmarks = new Map([
  ['recordings', [0, 0]],
  ['artists', [-2400, -1050]],
  ['albums', [-2500, 700]],
  ['track_sources', [2100, -350]],
  ['accounts', [2600, 1900]],
  ['playlist_scopes', [4100, 500]],
  ['genre_catalog', [-300, -1950]],
  ['play_events', [850, 1950]],
  ['vibe_definitions', [-650, 2200]],
  ['group_versions', [-3550, 2000]],
  ['recommendation_generations', [-4400, 200]],
  ['group_generations', [-4500, 1250]],
  ['recommendation_policies', [-5050, -900]],
  ['rec_model_selection_runs', [-3900, -1700]],
  ['metadata_deliveries', [2000, -2000]],
  ['metadata_observations', [850, -1250]],
  ['measurements', [-1600, 1950]],
  ['audio_annotations', [-1620, 3050]],
  ['predictions', [350, 2950]],
  ['artist_credits', [-1250, -1650]],
  ['album_track_occurrences', [-1300, 1100]],
  ['radio_sessions', [4350, 2250]],
  ['choice_learning_publications', [1250, 3300]],
  ['taste_axes', [-1550, -2450]],
]);
function nameAffinity(left, right) {
  const a = left.split('_'), b = right.split('_');
  let common = 0;
  while (common < Math.min(a.length, b.length) && a[common].replace(/s$/, '') === b[common].replace(/s$/, '')) common++;
  return common;
}

function preferredPlaces(entities, edges) {
  const nodes = new Map(entities.map(entity => [entity.id, {entity, parents: new Map(), children: new Set()}]));
  for (const {source, target} of edges) {
    const child = nodes.get(source), parent = nodes.get(target);
    if (!parent || parent === child) continue;
    child.parents.set(parent, 1 + .8 * nameAffinity(source, target));
    parent.children.add(child);
  }
  for (const [name, node] of nodes) {
    const anchor = landmarks.get(name);
    if (anchor) [node.x, node.y] = anchor;
    node.fixed = Boolean(anchor);
    if (node.fixed || node.parents.size || node.children.size) continue;
    // Use a view's named inputs as placement hints only. The canvas lines are
    // reserved for declared foreign keys.
    const inputs = node.entity.kind === 'view'
      ? [...node.entity.sql.matchAll(/\b(?:FROM|JOIN)\s+(?:"([^"]+)"|(\w+))/gi)] : [];
    for (const [, quoted, bare] of inputs) {
      const input = nodes.get((quoted || bare).toLowerCase());
      if (input && input !== node) node.parents.set(input, 1);
    }
    if (node.parents.size) continue;
    let family, affinity = 0;
    for (const [candidate, owner] of nodes) {
      if (!owner.fixed && !owner.children.size) continue;
      const score = nameAffinity(name, candidate);
      if (score > affinity || (score === affinity && score && owner.children.size > family.children.size)) {
        family = owner;
        affinity = score;
      }
    }
    if (family) node.parents.set(family, 1);
  }

  // Propagate landmark positions through the graph. A table referencing two
  // owners goes toward their midpoint; inbound references mainly position roots.
  const floating = [...nodes.values()].filter(node => !node.fixed);
  for (let round = 0; round < 24; round++) {
    const places = floating.map(node => {
      const peers = node.parents.size ? node.parents : [...node.children].map(child => [child, 1]);
      let x = 0, y = 0, total = 0;
      for (const [peer, weight] of peers) {
        if (peer.x === undefined) continue;
        x += peer.x * weight;
        y += peer.y * weight;
        total += weight;
      }
      // Some mutually referencing tables only reach a landmark through a child.
      if (!total && node.x === undefined) {
        for (const child of node.children) {
          if (child.x === undefined) continue;
          x += child.x;
          y += child.y;
          total++;
        }
      }
      return total && [x / total, y / total];
    });
    floating.forEach((node, i) => {
      if (places[i]) [node.x, node.y] = places[i];
    });
  }
  return [...nodes.values()].sort((a, b) => Number(b.fixed) - Number(a.fixed)
    || Number(b.x !== undefined) - Number(a.x !== undefined)
    || placementPriority(b) - placementPriority(a)
    || a.entity.id.localeCompare(b.entity.id));
}

function placementPriority(node) {
  const direct = [...node.parents].filter(([parent]) => parent.fixed);
  const affinity = Math.max(0, ...direct.map(([, weight]) => weight - 1));
  return Math.log2(1 + node.children.size) + (direct.length === 1 ? 2 * affinity + 2 : direct.length);
}

function placeCards(entities, edges, primaryEntities) {
  const nodes = preferredPlaces(entities, edges);
  const boxes = [];
  const occupied = new Set();
  const gap = 3;
  for (const node of nodes) {
    let centerX = node.x ?? 2900, centerY = node.y ?? 3450, weight = 1;
    if (!node.fixed) {
      for (const [parent, affinity] of node.parents) {
        if (!parent.box) continue;
        centerX += (parent.box.x + width / 2) * affinity;
        centerY += (parent.box.y + height / 2) * affinity;
        weight += affinity;
      }
    }
    const originX = centerX / weight / step, originY = centerY / weight / step;
    const phase = [...node.entity.id].reduce((hash, char) => (Math.imul(hash, 31) + char.charCodeAt(0)) >>> 0, 0);
    let left, top, clear;
    // Sample outward from each table's preferred position in card-sized
    // ellipses. The occupied cells reserve enough clearance to route around cards.
    for (let attempt = 0; !clear; attempt++) {
      const radius = .79 * Math.sqrt(attempt);
      const angle = goldenAngle * (attempt + phase % 997);
      left = Math.round(originX - width / step / 2 + Math.cos(angle) * radius * (width / step + gap));
      top = Math.round(originY - height / step / 2 + Math.sin(angle) * radius * (height / step + gap));
      clear = true;
      for (let y = top; clear && y <= top + height / step; y++) {
        for (let x = left; x <= left + width / step; x++) {
          if (occupied.has(`${x},${y}`)) { clear = false; break; }
        }
      }
    }
    for (let y = top - gap; y <= top + height / step + gap; y++) {
      for (let x = left - gap; x <= left + width / step + gap; x++) occupied.add(`${x},${y}`);
    }
    node.box = {node: node.entity, primary: primaryEntities.has(node.entity.id),
      x: left * step, y: top * step, width, height};
    boxes.push(node.box);
  }
  return boxes;
}

export function layoutSchema(entities, edges, primaryEntities) {
  const placed = placeCards(entities, edges, primaryEntities);
  const dx = margin - Math.min(0, ...placed.map(box => box.x));
  const dy = margin - Math.min(0, ...placed.map(box => box.y));
  const boxes = new Map();
  for (const box of placed) {
    box.x += dx;
    box.y += dy;
    boxes.set(box.node.id, box);
  }
  const width = Math.max(margin, ...placed.map(box => box.x + box.width)) + margin;
  const height = Math.max(margin, ...placed.map(box => box.y + box.height)) + margin;
  return {boxes, paths: routeForeignKeys(boxes, edges, width, height), width, height};
}

export function placeLabels(anchors, width, height) {
  // Screen-space labels need their own bounds when the cards fit at a small scale.
  const placed = new Map();
  const padding = 12, minY = 64, maxY = height - 60;
  const separation = 28;
  const clamp = (value, min, max) => Math.max(min, Math.min(value, max));
  for (const anchor of anchors) {
    const minX = padding + anchor.halfWidth, maxX = width - minX;
    if (minX > maxX || minY > maxY) continue;
    const x = clamp(anchor.x, minX, maxX), y = clamp(anchor.y, minY, maxY);
    const labels = [...placed.values()];
    const rows = [y, ...labels.flatMap(label => [label.y - separation, label.y + separation])]
      .filter(row => row >= minY && row <= maxY).sort((a, b) => Math.abs(a - anchor.y) - Math.abs(b - anchor.y));
    let best, distance = Infinity;
    for (const row of rows) {
      const verticalDistance = (row - anchor.y) ** 2;
      if (verticalDistance >= distance) break;
      const blocked = labels.filter(label => Math.abs(label.y - row) < separation).map(label => {
        const space = label.halfWidth + anchor.halfWidth + 5;
        return {left: label.x - space, right: label.x + space};
      });
      for (const column of [x, ...blocked.flatMap(({left, right}) => [left, right])]) {
        const candidate = (column - anchor.x) ** 2 + verticalDistance;
        if (candidate >= distance || column < minX || column > maxX) continue;
        if (blocked.some(({left, right}) => column > left && column < right)) continue;
        best = {x: column, y: row, halfWidth: anchor.halfWidth};
        distance = candidate;
      }
    }
    if (best) placed.set(anchor.id, best);
  }
  return placed;
}
