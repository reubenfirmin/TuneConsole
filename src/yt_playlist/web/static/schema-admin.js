/* Search, navigation and the camera for the read-only SQLite diagram. */
import {layoutSchema, placeLabels} from './schema-admin-layout.js';
import {entityDescriptions, installedFilters} from './schema-admin-filters.js';

function mountSchemaMap(root, signal) {
  const get = id => root.querySelector(`#sd-${id}`);
  const {entities, edges} = JSON.parse(get('data').textContent);
  const byId = new Map(entities.map(entity => [entity.id, entity]));
  const filters = installedFilters(byId, edges);
  const links = new Map(entities.map(entity => [entity.id, []]));
  for (const edge of edges) {
    links.get(edge.source).push(edge);
    if (edge.source !== edge.target) links.get(edge.target)?.push(edge);
  }

  const canvas = get('canvas'), filter = get('filter'), search = get('search'), results = get('results');
  const edgeLayer = get('edges'), nodeLayer = get('nodes'), labels = get('labels'), detail = get('detail');
  const drawing = layoutSchema(entities, edges, filters);
  const cards = new Map(), landmarkLabels = new Map();
  let selected, camera, drag, hovered, visible, filterBounds;
  const count = (n, noun) => `${n} ${noun}${n === 1 ? '' : 's'}`;
  const inView = id => !visible || visible.has(id);
  const kind = entity => entity.kind === 'virtual' ? 'Virtual table' : entity.kind === 'view' ? 'View' : 'Table';
  const preview = node => [...node.columns].sort((a, b) => Number(Boolean(b.pk || b.fk)) - Number(Boolean(a.pk || a.fk))).slice(0, 3);

  function updateInventory(edgeCount) {
    const shown = entities.filter(entity => inView(entity.id));
    const views = shown.filter(entity => entity.kind === 'view').length;
    get('inventory').textContent = [count(shown.length - views, 'table'), count(views, 'view'),
      count(edgeCount, 'foreign key')].join(' · ');
  }

  function svg(tag, attributes, value) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [name, content] of Object.entries(attributes)) element.setAttribute(name, content);
    if (value !== undefined) element.textContent = value;
    return element;
  }

  function html(tag, text, className) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function action(label, handler, className) {
    const button = html('button', label, className);
    button.type = 'button';
    button.addEventListener('click', handler);
    return button;
  }

  function paintPath({edge, points, missing}) {
    const path = svg('polyline', {class: 'sd-edge', 'data-edge': edge.id,
      points: points.map(point => point.join(',')).join(' ')});
    path.setAttribute('marker-start', edge.many ? 'url(#sd-many)' : 'url(#sd-one)');
    if (missing) path.classList.add('sd-edge-missing');
    else path.setAttribute('marker-end', 'url(#sd-parent)');
    const label = `${edge.source} (${edge.columns.join(', ')}) references ${edge.target} (${edge.referenced_columns.join(', ')})`;
    path.append(svg('title', {}, label));
    return path;
  }

  function paintCard({node, primary, x, y, width, height}) {
    const group = svg('g', {class: 'sd-card', transform: `translate(${x} ${y})`, role: 'button', tabindex: '0',
      'aria-label': `${node.id}, ${kind(node).toLowerCase()}`, 'aria-pressed': 'false'});
    group.dataset.entity = node.id;
    if (primary) group.classList.add('sd-primary');
    if (node.kind === 'view') group.classList.add('sd-view');
    group.append(svg('rect', {class: 'sd-card-box', width, height, rx: 5}));
    group.append(svg('path', {class: 'sd-card-accent', d: `M2 8 V${height - 8}`}));
    const titleLimit = (width - 32) / (.61 * node.id.length);
    group.append(svg('text', {class: 'sd-card-title', x: 16, y: 27, 'font-size': Math.min(14, titleLimit),
      style: `--sd-title-limit: ${titleLimit}px`}, node.id));
    group.append(svg('text', {class: 'sd-card-meta', x: 16, y: 46},
      `${kind(node)} · ${count(node.columns.length, 'column')}`));
    group.append(svg('path', {class: 'sd-card-divider', d: `M0 56 H${width}`}));
    preview(node).forEach((column, index) => {
      const row = 76 + index * 16;
      const keys = [column.pk ? 'PK' : '', column.fk ? 'FK' : ''].filter(Boolean).join(' ');
      group.append(svg('text', {class: 'sd-card-key', x: 16, y: row}, keys));
      const name = column.name.length > 37 ? `${column.name.slice(0, 36)}…` : column.name;
      group.append(svg('text', {class: 'sd-card-column', x: 54, y: row}, name));
    });
    group.append(svg('title', {}, node.id));
    group.addEventListener('click', () => show(node.id));
    group.addEventListener('keydown', event => {
      if (!['Enter', ' '].includes(event.key)) return;
      event.preventDefault();
      show(node.id);
      detail.querySelector('button').focus();
    });
    group.addEventListener('pointerenter', () => {
      if (!root.classList.contains('sd-overview')) { hovered = node.id; trace(); }
    });
    group.addEventListener('pointerleave', () => { hovered = null; trace(); });
    return group;
  }

  function addLabel(name, primary) {
    const width = name.length * 7.55 + 15;
    const label = svg('g', {class: 'sd-landmark', 'aria-hidden': 'true'});
    if (!primary) label.classList.add('sd-secondary-landmark');
    label.append(svg('path', {class: 'sd-label-leader'}));
    label.append(svg('rect', {x: -width / 2, y: -12, width, height: 24, rx: 5}));
    label.append(svg('text', {x: 0, y: 4, 'text-anchor': 'middle'}, name));
    label.addEventListener('click', () => show(name));
    landmarkLabels.set(name, label);
    labels.append(label);
  }

  function trace() {
    const id = hovered || selected;
    const related = new Set([id]);
    for (const edge of links.get(id) || []) related.add(edge.source === id ? edge.target : edge.source);
    root.classList.toggle('sd-tracing', Boolean(id));
    for (const path of drawing.paths) {
      path.element.classList.toggle('sd-highlight', path.edge.source === id || path.edge.target === id);
    }
    for (const [name, card] of cards) {
      card.classList.toggle('sd-related', related.has(name));
      card.classList.toggle('sd-selected', name === selected);
      card.setAttribute('aria-pressed', String(name === selected));
    }
  }

  function renderDetails(entity) {
    detail.hidden = !entity;
    detail.replaceChildren();
    if (!entity) return;
    const top = html('div', undefined, 'sd-detail-top');
    top.append(html('p', kind(entity)),
      action('Close details', () => { show(); cards.get(entity.id).focus({preventScroll: true}); }, 'sd-close'));
    detail.append(top, html('h2', entity.id));
    const description = entityDescriptions.get(entity.id);
    if (description) detail.append(html('p', description, 'sd-entity-description'));
    const fields = html('table', undefined, 'sd-columns');
    const headers = fields.createTHead().insertRow();
    for (const title of ['Column', 'Type', 'Key']) {
      const header = html('th', title);
      header.scope = 'col';
      headers.append(header);
    }
    const body = fields.createTBody();
    for (const column of entity.columns) {
      const row = body.insertRow();
      row.insertCell().textContent = column.name;
      const type = row.insertCell();
      type.textContent = column.type || '—';
      type.title = [column.required ? 'NOT NULL' : '', column.hidden >= 2 ? 'Generated' : '',
        column.default === null ? '' : `DEFAULT ${column.default}`].filter(Boolean).join(' · ');
      row.insertCell().textContent = [column.pk ? 'PK' : '', column.fk ? 'FK' : ''].filter(Boolean).join(' / ');
    }
    detail.append(fields);
    const relations = links.get(entity.id);
    for (const [title, connections] of [
      ['References', relations.filter(edge => edge.source === entity.id)],
      ['Referenced by', relations.filter(edge => edge.target === entity.id && edge.source !== entity.id)],
    ]) {
      if (!connections.length) continue;
      detail.append(html('h3', title));
      for (const edge of connections) {
        const other = edge.source === entity.id ? edge.target : edge.source;
        const label = `${edge.source} (${edge.columns.join(', ')}) references ${edge.target} (${edge.referenced_columns.join(', ')})`;
        const row = html('div', undefined, 'sd-reference');
        if (byId.has(other) && other !== entity.id && inView(other)) {
          row.append(action(other, () => { show(other); detail.querySelector('button').focus(); }));
        }
        else row.append(html('strong', other));
        const actions = [['UPDATE', edge.on_update], ['DELETE', edge.on_delete]]
          .filter(([, value]) => value !== 'NO ACTION').map(([operation, value]) => `ON ${operation} ${value}`);
        row.append(html('small', [label, ...actions].join(' · ')));
        detail.append(row);
      }
    }
    const ddl = html('details', undefined, 'sd-definition');
    ddl.append(html('summary', 'SQL definition'), html('pre', entity.sql || ''));
    detail.append(ddl);
    detail.scrollTop = 0;
  }

  function drawCamera(repositionLabels = true) {
    canvas.setAttribute('viewBox', `${camera.x} ${camera.y} ${camera.w} ${camera.h}`);
    const scale = canvas.clientWidth / camera.w;
    root.classList.toggle('sd-overview', scale < .30);
    root.classList.toggle('sd-zoomed-out', scale < .65);
    root.style.setProperty('--sd-scale', scale);
    if (repositionLabels) positionLabels(scale);
  }

  function positionLabels(scale) {
    const fitLabels = scale < (visible ? .65 : .30);
    const viewportWidth = canvas.clientWidth, viewportHeight = canvas.clientHeight;
    const anchors = [];
    for (const [id, label] of landmarkLabels) {
      if (!inView(id)) continue;
      const {x, y, width, height, primary} = drawing.boxes.get(id);
      const centerX = x + width / 2, centerY = y + height / 2;
      const screenX = (centerX - camera.x) * scale, screenY = (centerY - camera.y) * scale;
      const onScreen = screenX >= 0 && screenX <= viewportWidth && screenY >= 0 && screenY <= viewportHeight;
      if (fitLabels && onScreen) {
        anchors.push({id, x: screenX, y: screenY,
          halfWidth: label.querySelector('rect').width.baseVal.value / 2,
          priority: id === filter.value ? 2 : Number(primary)});
      } else {
        label.setAttribute('transform', `translate(${centerX} ${centerY}) scale(${1 / scale})`);
        label.firstElementChild.removeAttribute('d');
        label.removeAttribute('hidden');
      }
    }
    if (!fitLabels) return;
    anchors.sort((a, b) => b.priority - a.priority);
    const positions = placeLabels(anchors, viewportWidth, viewportHeight);
    for (const anchor of anchors) {
      const label = landmarkLabels.get(anchor.id);
      const position = positions.get(anchor.id);
      label.toggleAttribute('hidden', !position);
      if (!position) continue;
      label.setAttribute('transform', `translate(${camera.x + position.x / scale} ${camera.y + position.y / scale}) scale(${1 / scale})`);
      const dx = anchor.x - position.x, dy = anchor.y - position.y;
      const tx = Math.max(-anchor.halfWidth, Math.min(dx, anchor.halfWidth));
      const ty = Math.max(-12, Math.min(dy, 12));
      label.firstElementChild.setAttribute('d', dx === tx && dy === ty ? '' : `M${dx} ${dy} H${tx} V${ty}`);
    }
  }

  function fit() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    const {left, top, right, bottom} = filterBounds || {left: 0, top: 0, right: drawing.width, bottom: drawing.height};
    const ratio = Math.min((w - (filterBounds ? 24 : 0)) / (right - left),
      (h - (filterBounds ? 80 : 0)) / (bottom - top), 1.1);
    frame((left + right) / 2, (top + bottom) / 2, ratio);
  }

  function frame(x, y, scale) {
    const w = canvas.clientWidth / scale, h = canvas.clientHeight / scale;
    if (!w || !h) return;
    camera = {x: x - w / 2, y: y - h / 2, w, h, viewportWidth: canvas.clientWidth};
    drawCamera();
  }

  function reframe() {
    if (!camera || !canvas.clientWidth) return;
    frame(camera.x + camera.w / 2, camera.y + camera.h / 2, camera.viewportWidth / camera.w);
  }

  function frameEntity(id) {
    const box = drawing.boxes.get(id);
    const neighbors = new Set(links.get(id).map(edge => edge.source === id ? edge.target : edge.source).filter(inView));
    neighbors.delete(id);
    // Show both ends when inspecting a small relationship that fits at a readable scale.
    const scale = Math.max(.93, camera ? camera.viewportWidth / camera.w : 0);
    if (neighbors.size && neighbors.size <= 2 && scale < 1.15) {
      const connected = [box, ...[...neighbors].map(name => drawing.boxes.get(name)).filter(Boolean)];
      const left = Math.min(...connected.map(card => card.x)), top = Math.min(...connected.map(card => card.y));
      const right = Math.max(...connected.map(card => card.x + card.width));
      const bottom = Math.max(...connected.map(card => card.y + card.height));
      const fitScale = Math.min(canvas.clientWidth / (right - left + 145), canvas.clientHeight / (bottom - top + 180), scale);
      if (fitScale >= .47) {
        frame((left + right) / 2, (top + bottom) / 2, fitScale);
        return;
      }
    }
    frame(box.x + box.width / 2, box.y + box.height / 2, scale);
  }

  function zoom(factor, x = .5, y = .5) {
    if (!camera) return;
    const maximum = Math.max(drawing.width, drawing.height * camera.w / camera.h) * 3;
    const w = Math.max(canvas.clientWidth / 3, Math.min(maximum, camera.w * factor));
    const h = camera.h * w / camera.w;
    camera = {...camera, x: camera.x + (camera.w - w) * x, y: camera.y + (camera.h - h) * y, w, h};
    drawCamera();
  }

  function show(id, updateUrl = true) {
    const entity = inView(id) ? byId.get(id) : undefined;
    selected = entity?.id;
    hovered = null;
    trace();
    renderDetails(entity);
    root.dataset.mode = entity ? 'entity' : 'overview';
    if (entity) frameEntity(entity.id);
    else if (camera) reframe();
    else fit();
    if (updateUrl) {
      const hash = entity ? `#entity=${encodeURIComponent(entity.id)}` : '';
      history.replaceState(history.state, '', location.pathname + location.search + hash);
    }
  }

  function applyFilter() {
    const id = filter.value;
    visible = filters.get(id) || null;
    root.classList.toggle('sd-filtered', Boolean(id));
    const points = [];
    for (const [name, card] of cards) {
      const shown = inView(name);
      card.toggleAttribute('hidden', !shown);
      const {x, y, width, height, primary} = drawing.boxes.get(name);
      if (primary) {
        landmarkLabels.get(name).toggleAttribute('hidden', !shown);
      } else if (id && shown) {
        if (!landmarkLabels.has(name)) addLabel(name, false);
      } else if (landmarkLabels.has(name)) {
        landmarkLabels.get(name).remove();
        landmarkLabels.delete(name);
      }
      if (id && shown) {
        points.push([x, y], [x + width, y + height]);
      }
    }
    let edgeCount = 0;
    for (const path of drawing.paths) {
      const shown = inView(path.edge.source) && inView(path.edge.target);
      path.element.toggleAttribute('hidden', !shown);
      if (shown) {
        edgeCount++;
        if (id) points.push(...path.points);
      }
    }
    filterBounds = id ? {
      left: Math.min(...points.map(([x]) => x)) - 85,
      top: Math.min(...points.map(([, y]) => y)) - 130,
      right: Math.max(...points.map(([x]) => x)) + 85,
      bottom: Math.max(...points.map(([, y]) => y)) + 80,
    } : null;
    updateInventory(edgeCount);
    search.value = '';
    results.hidden = true;
    camera = undefined;
    show();
  }

  function choose(entity) {
    search.value = '';
    results.hidden = true;
    show(entity.id);
    detail.querySelector('button').focus();
  }

  function matches() {
    const query = search.value.trim().toLowerCase();
    if (!query) return [];
    const hits = entities.filter(entity => inView(entity.id) && entity.id.toLowerCase().includes(query));
    return hits.sort((a, b) => Number(!a.id.toLowerCase().startsWith(query)) - Number(!b.id.toLowerCase().startsWith(query)));
  }

  function offerMatches() {
    const hits = matches();
    results.hidden = !search.value.trim();
    results.replaceChildren(...hits.slice(0, 20).map(entity => {
      const option = action(entity.id, () => choose(entity));
      option.append(html('small', kind(entity)));
      return option;
    }));
    if (!hits.length && !results.hidden) results.append(html('p', 'No matching tables or views.'));
  }

  search.addEventListener('input', offerMatches);
  search.addEventListener('focus', offerMatches);
  search.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      const first = matches()[0];
      if (first) { event.preventDefault(); choose(first); }
    }
    if (event.key === 'ArrowDown' && !results.hidden) {
      event.preventDefault();
      results.querySelector('button')?.focus();
    }
  });
  root.querySelector('.sd-search').addEventListener('focusout', event => {
    if (!event.currentTarget.contains(event.relatedTarget)) results.hidden = true;
  });
  root.querySelector('.sd-search').addEventListener('keydown', event => {
    if (event.key === 'Escape') { search.focus(); results.hidden = true; }
  });
  get('fit').addEventListener('click', fit);
  get('zoom-in').addEventListener('click', () => zoom(.8));
  get('zoom-out').addEventListener('click', () => zoom(1.25));
  canvas.addEventListener('wheel', event => {
    if (!camera) return;
    event.preventDefault();
    const bounds = canvas.getBoundingClientRect();
    zoom(Math.exp(Math.max(-100, Math.min(100, event.deltaY)) * .004),
      (event.clientX - bounds.left) / bounds.width, (event.clientY - bounds.top) / bounds.height);
  }, {passive: false});
  canvas.addEventListener('pointerdown', event => {
    if (!camera || event.button !== 0 || event.target.closest('.sd-card, .sd-landmark')) return;
    canvas.setPointerCapture(event.pointerId);
    drag = {x: event.clientX, y: event.clientY, camX: camera.x, camY: camera.y};
    canvas.classList.add('sd-dragging');
  });
  canvas.addEventListener('pointermove', event => {
    if (!drag) return;
    camera.x = drag.camX - (event.clientX - drag.x) * camera.w / canvas.clientWidth;
    camera.y = drag.camY - (event.clientY - drag.y) * camera.h / canvas.clientHeight;
    drawCamera(false);
  });
  canvas.addEventListener('lostpointercapture', () => { drag = null; canvas.classList.remove('sd-dragging'); });
  canvas.addEventListener('keydown', event => {
    if (event.target !== canvas || !camera) return;
    const pan = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]}[event.key];
    if (pan) {
      event.preventDefault();
      camera.x += pan[0] * camera.w / 12;
      camera.y += pan[1] * camera.h / 12;
      drawCamera(false);
    } else if (['+', '=', '-', '0'].includes(event.key)) {
      event.preventDefault();
      if (event.key === '0') fit();
      else zoom(event.key === '-' ? 1.25 : .8);
    }
  });
  function fromUrl() {
    const id = new URLSearchParams(location.hash.slice(1)).get('entity');
    if (byId.has(id) ? selected !== id : selected || !camera) show(id, false);
  }
  for (const path of drawing.paths) {
    path.element = paintPath(path);
    edgeLayer.append(path.element);
  }
  for (const box of drawing.boxes.values()) {
    const card = paintCard(box);
    cards.set(box.node.id, card);
    nodeLayer.append(card);
    if (box.primary) addLabel(box.node.id, true);
  }
  for (const entity of entities) {
    if (!filters.has(entity.id)) continue;
    const option = html('option', entity.id);
    option.value = entity.id;
    filter.append(option);
  }
  filter.value = '';
  filter.addEventListener('change', applyFilter);
  updateInventory(edges.length);
  const resize = new ResizeObserver(() => camera ? reframe() : fit());
  resize.observe(canvas);
  signal.addEventListener('abort', () => resize.disconnect(), {once: true});
  window.addEventListener('hashchange', fromUrl, {signal});
  fromUrl();
}

let activeRoot, events;
function unmount() {
  events?.abort();
  activeRoot = null;
}
function mount() {
  const root = document.getElementById('schema-map');
  if (!root || activeRoot === root) return;
  unmount();
  activeRoot = root;
  events = new AbortController();
  mountSchemaMap(root, events.signal);
}
window.addEventListener('app-page-leaving', unmount);
window.addEventListener('app-page-changed', mount);
mount();
