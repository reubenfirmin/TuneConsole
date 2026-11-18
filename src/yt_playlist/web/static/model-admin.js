/* Exploration state and popup details; the renderer owns force layout and painting. */
(() => {
  let dispose = () => {};
  function mount() {
    dispose();
    const root = document.getElementById('model-map');
    if (!root) return;
    const data = JSON.parse(root.querySelector('#mm-data').textContent);
    const nodes = new Map(data.nodes.map(node => [node.id, node]));
    const incoming = new Map(data.nodes.map(node => [node.id, []]));
    const outgoing = new Map(data.nodes.map(node => [node.id, []]));
    data.edges.forEach((edge, index) => { incoming.get(edge.target).push(index); outgoing.get(edge.source).push(index); });
    const canvas = root.querySelector('#mm-canvas'), dialog = root.querySelector('#mm-dialog');
    const detail = root.querySelector('#mm-detail'), query = root.querySelector('#mm-search');
    const area = root.querySelector('#mm-area'), layer = root.querySelector('#mm-layer');
    const isolate = root.querySelector('#mm-isolate'), results = root.querySelector('#mm-results');
    const tooltip = root.querySelector('.mm-hover'), empty = root.querySelector('.mm-empty');
    const events = new AbortController();
    const on = (element, event, handler, options = {}) => element.addEventListener(event, handler, {...options, signal: events.signal});
    let anchor = null, hovered = null, renderer = null, previousVisible = '';

    // Two linear walks trace actual ancestors and descendants, without lighting
    // sibling branches merely because they share a dependency.
    function flow(id) {
      const related = new Set(id ? [id] : []), edges = new Set();
      if (!id) return {related, edges};
      for (const [adjacency, next] of [[incoming, 'source'], [outgoing, 'target']]) {
        const queue = [id], visited = new Set(queue);
        for (let cursor = 0; cursor < queue.length; cursor++) for (const index of adjacency.get(queue[cursor])) {
          edges.add(index);
          const neighbor = data.edges[index][next]; related.add(neighbor);
          if (!visited.has(neighbor)) { visited.add(neighbor); queue.push(neighbor); }
        }
      }
      return {related, edges};
    }
    function highlight(id) {
      root.dataset.selected = id || '';
      renderer?.trace(id, flow(id));
    }
    function hover(id) {
      if (dialog.open) return;
      hovered = id;
      highlight(id || anchor);
      tooltip.hidden = !id;
      if (id) {
        const node = nodes.get(id);
        tooltip.querySelector('strong').textContent = node.title;
        tooltip.querySelector('span').textContent = node.summary;
        positionTooltip();
      }
    }
    function positionTooltip() {
      if (!hovered || tooltip.hidden || !renderer) return;
      const node = renderer.graph.graphData().nodes.find(value => value.id === hovered);
      if (!node) { tooltip.hidden = true; return; }
      const point = renderer.graph.graph2ScreenCoords(node.x, node.y);
      const x = Math.max(12, Math.min(canvas.clientWidth - tooltip.offsetWidth - 12, point.x + 18));
      const y = point.y + 50 + tooltip.offsetHeight < canvas.clientHeight - 70
        ? point.y + 50 : point.y - tooltip.offsetHeight - 40;
      tooltip.style.left = `${x}px`; tooltip.style.top = `${Math.max(75, y)}px`;
    }
    function hideResults() { results.hidden = true; query.setAttribute('aria-expanded', 'false'); }
    function matching() {
      const term = query.value.trim().toLocaleLowerCase();
      return data.nodes.filter(node => (!area.value || node.group === area.value)
        && (!layer.value || node.layers.includes(layer.value))
        && (!term || [node.title, node.summary, node.kind, node.group, node.app_area].some(value => value.toLocaleLowerCase().includes(term))));
    }
    function showResults(force = false) {
      if (!query.value.trim() && !force) { hideResults(); return; }
      const matches = new Set(matching().map(node => node.id));
      for (const button of results.children) button.hidden = !matches.has(button.dataset.inspect);
      results.hidden = !matches.size;
      query.setAttribute('aria-expanded', String(!results.hidden));
    }
    function filter() {
      const matches = new Set(matching().map(node => node.id)), visible = new Set(matches);
      if (query.value.trim() || area.value || layer.value) data.edges.forEach(edge => {
        if (matches.has(edge.source)) visible.add(edge.target);
        if (matches.has(edge.target)) visible.add(edge.source);
      });
      if (isolate.checked && anchor) {
        const traced = flow(anchor);
        for (const id of visible) if (!traced.related.has(id)) visible.delete(id);
      }
      const key = [...visible].sort().join(',');
      if (key !== previousVisible) { previousVisible = key; renderer.update(visible); }
      empty.hidden = visible.size !== 0;
      root.querySelector('#mm-visible-count').textContent = `${visible.size} of ${nodes.size}`;
      hover(null);
    }
    function open(id) {
      if (!nodes.has(id)) return;
      anchor = id; hovered = null; tooltip.hidden = true; hideResults();
      isolate.disabled = false;
      detail.replaceChildren(root.querySelector(`#mm-detail-${id}`).content.cloneNode(true));
      highlight(id);
      if (!dialog.open) dialog.showModal();
      dialog.scrollTop = 0;
      root.querySelector('#mm-close').focus({preventScroll: true});
      history.replaceState(history.state, '', location.pathname + location.search + `#${encodeURIComponent(id)}`);
    }
    function clear() {
      if (dialog.open) dialog.close();
      query.value = ''; area.value = ''; layer.value = ''; anchor = null; hovered = null;
      isolate.checked = false; isolate.disabled = true; hideResults(); highlight(null); filter();
      renderer.resetView();
      history.replaceState(history.state, '', location.pathname + location.search);
    }
    renderer = window.createModelMapRenderer(canvas, data, {hover, open,
      background() { if (!isolate.checked) { anchor = null; isolate.disabled = true; highlight(null); } },
      frame: positionTooltip,
    });
    root.modelGraph = renderer.graph;
    isolate.disabled = true;
    on(root, 'click', event => {
      const target = event.target.closest('[data-inspect]');
      if (target) open(target.dataset.inspect);
    });
    on(root.querySelector('#mm-close'), 'click', () => dialog.close());
    on(dialog, 'close', () => { highlight(anchor); canvas.focus({preventScroll: true}); });
    on(dialog, 'click', event => {
      const bounds = dialog.getBoundingClientRect();
      if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right
          || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
    });
    on(root.querySelector('#mm-focus'), 'click', () => {
      query.value = ''; area.value = ''; layer.value = ''; isolate.checked = true;
      dialog.close(); filter(); renderer.center(anchor);
    });
    on(query, 'input', () => { filter(); showResults(); });
    on(query, 'focus', () => showResults());
    on(query, 'keydown', event => {
      if (event.key === 'ArrowDown') { event.preventDefault(); showResults(true); results.querySelector('button:not([hidden])')?.focus(); }
      if (event.key === 'Enter') { const first = matching()[0]; if (first) { event.preventDefault(); open(first.id); } }
    });
    on(results, 'keydown', event => {
      if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
      event.preventDefault();
      const buttons = [...results.querySelectorAll('button:not([hidden])')], index = buttons.indexOf(document.activeElement);
      buttons[(index + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length]?.focus();
    });
    on(document, 'pointerdown', event => { if (!event.target.closest('.mm-search')) hideResults(); });
    for (const select of [area, layer]) on(select, 'change', () => { hideResults(); filter(); });
    on(isolate, 'change', filter);
    on(root.querySelector('#mm-reset'), 'click', clear);
    on(root.querySelector('#mm-fit'), 'click', () => renderer.fit());
    function zoom(factor) { renderer.graph.zoom(Math.max(.15, Math.min(3.5, renderer.graph.zoom() * factor)), 180); }
    on(root.querySelector('#mm-zoom-in'), 'click', () => zoom(1.25));
    on(root.querySelector('#mm-zoom-out'), 'click', () => zoom(.8));
    on(canvas, 'pointerdown', hideResults);
    on(canvas, 'keydown', event => {
      if (['+', '=', '-', '0'].includes(event.key)) {
        event.preventDefault();
        if (event.key === '0') renderer.fit(); else zoom(event.key === '-' ? .8 : 1.25);
      }
    });
    on(root, 'keydown', event => {
      if (event.key !== 'Escape' || dialog.open) return;
      event.preventDefault(); hideResults();
      anchor = null; isolate.checked = false; isolate.disabled = true; highlight(null); filter();
      history.replaceState(history.state, '', location.pathname + location.search);
    });
    filter();
    const initial = location.hash.slice(1);
    if (nodes.has(initial)) open(initial);
    dispose = () => {
      events.abort();
      if (dialog.open) dialog.close();
      renderer.destroy(); delete root.modelGraph;
    };
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, {once: true});
  else mount();
  window.addEventListener('app-page-changed', mount);
  window.addEventListener('app-page-leaving', () => { dispose(); dispose = () => {}; });
})();
