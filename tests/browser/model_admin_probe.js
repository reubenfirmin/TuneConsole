// Browser-side observations for the model-map tests. UI actions stay in Playwright.
async ({action, id, zoom, expected}) => {
  const root = document.querySelector('#model-map'), host = document.querySelector('#mm-canvas');
  const graph = root?.modelGraph;
  const view = () => ({zoom: graph.zoom(), center: graph.centerAt()});
  const actions = {
    position() {
      const bounds = host.getBoundingClientRect();
      const node = graph.graphData().nodes.find(node => node.id === id);
      const point = graph.graph2ScreenCoords(node.x, node.y);
      return {x: bounds.left + point.x, y: bounds.top + point.y};
    },
    background() {
      const bounds = host.getBoundingClientRect(), scale = graph.zoom();
      const positions = graph.graphData().nodes.map(node => ({...graph.graph2ScreenCoords(node.x, node.y),
        width: node.width * scale, height: node.height * scale}));
      for (let y = 130; y < bounds.height - 150; y += 40) {
        for (let x = bounds.width - 40; x > 140; x -= 40) {
          if (positions.some(point => Math.abs(point.x - x) < point.width / 2 + 20 && Math.abs(point.y - y) < point.height / 2 + 20)) continue;
          const point = {x: bounds.left + x, y: bounds.top + y};
          if (document.elementFromPoint(point.x, point.y)?.tagName === 'CANVAS') return point;
        }
      }
      throw new Error('No empty canvas position for the pan check');
    },
    ids: () => graph.graphData().nodes.map(node => node.id),
    zoom: () => graph.zoom(),
    view,
    visibleConnections() {
      const nodes = graph.graphData().nodes, scale = graph.zoom();
      const bounds = host.getBoundingClientRect();
      const visible = new Set(nodes.filter(node => {
        const point = graph.graph2ScreenCoords(node.x, node.y);
        return point.x - node.width * scale / 2 >= 0 && point.x + node.width * scale / 2 <= bounds.width
          && point.y - node.height * scale / 2 >= 130 && point.y + node.height * scale / 2 <= bounds.height - 80;
      }).map(node => node.id));
      return graph.graphData().links.filter(link => link.source.area === 'model' && link.target.area === 'feature'
        && visible.has(link.source.id) && visible.has(link.target.id))
        .map(link => [link.source.id, link.target.id]);
    },
    featureAreas() {
      const nodes = graph.graphData().nodes;
      return [...root.querySelectorAll('[data-mm-feature-area]')].map(element => {
        const bounds = element.getBoundingClientRect();
        const members = nodes.filter(node => node.app_area === element.dataset.mmFeatureArea);
        const hostBounds = host.getBoundingClientRect(), scale = graph.zoom();
        return {title: element.querySelector('h2').textContent, hidden: element.hidden, width: bounds.width,
          members: members.map(node => {
            const point = graph.graph2ScreenCoords(node.x, node.y);
            return {id: node.id, top: node.y - node.height / 2, bottom: node.y + node.height / 2,
              contained: point.x + hostBounds.left - node.width * scale / 2 >= bounds.left
                && point.x + hostBounds.left + node.width * scale / 2 <= bounds.right
                && point.y + hostBounds.top - node.height * scale / 2 >= bounds.top
                && point.y + hostBounds.top + node.height * scale / 2 <= bounds.bottom};
          })};
      });
    },
    featureRouting() {
      const nodes = graph.graphData().nodes.filter(node => node.area === 'feature');
      const routes = window.routeModelMapFeatures(nodes), blocked = [];
      for (const [identity, path] of routes) for (let index = 1; index < path.length; index++) {
        const a = path[index - 1], b = path[index], steps = Math.ceil(Math.hypot(b.x - a.x, b.y - a.y) / 4);
        for (let step = 0; step <= steps; step++) {
          const x = a.x + (b.x - a.x) * step / steps, y = a.y + (b.y - a.y) * step / steps;
          for (const node of nodes) {
            if (node.id !== identity && Math.abs(x - node.x) < node.width / 2 && Math.abs(y - node.y) < node.height / 2)
              blocked.push([identity, node.id]);
          }
        }
      }
      return {count: routes.size, blocked};
    },
    linkPaint() {
      const link = graph.graphData().links.find(value => value.source.id === id && value.target.id === expected);
      const context = document.createElement('canvas').getContext('2d');
      let result;
      context.stroke = () => { result = {alpha: context.globalAlpha, color: context.strokeStyle}; };
      graph.linkCanvasObject()(link, context);
      return result;
    },
    nodePaint() {
      const node = graph.graphData().nodes.find(value => value.id === id);
      const context = document.createElement('canvas').getContext('2d');
      const fills = [], labels = [], outlines = [];
      context.fill = () => fills.push(context.fillStyle);
      context.stroke = () => outlines.push(context.strokeStyle);
      context.fillText = text => labels.push(text);
      graph.nodeCanvasObject()(node, context);
      return {fill: fills[0], outline: outlines[0], caption: labels.at(-1)};
    },
    origin: () => graph.graph2ScreenCoords(0, 0),
    zoomed: () => graph.zoom() > zoom,
    reset() {
      const current = view();
      return Math.abs(current.zoom - expected.zoom) < .001
        && Math.hypot(current.center.x - expected.center.x, current.center.y - expected.center.y) < .01;
    },
    viewport() {
      return {overflowX: document.documentElement.scrollWidth > innerWidth,
        overflowY: document.documentElement.scrollHeight > innerHeight, scrollY};
    },
    topology() {
      const nodes = graph.graphData().nodes;
      const edges = JSON.parse(root.querySelector('#mm-data').textContent).edges;
      const byId = new Map(nodes.map(node => [node.id, node]));
      const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
      return {models: nodes.filter(node => node.area === 'model').map(node => Math.hypot(node.x, node.y)),
        features: nodes.filter(node => node.area === 'feature').map(node => node.x),
        featureLeft: Math.min(...nodes.filter(node => node.area === 'feature').map(node => node.x - node.width / 2)),
        modelRight: Math.max(...nodes.filter(node => node.area === 'model').map(node => node.x + node.width / 2)),
        modelColumns: nodes.filter(node => node.area === 'model').map(node => node.x),
        gaps: edges.map(edge => {
          const source = byId.get(edge.source), target = byId.get(edge.target);
          return target.x - source.x - (source.width + target.width) / 2;
        }),
        overlaps: nodes.flatMap((node, index) => nodes.slice(index + 1).filter(other =>
          Math.abs(node.x - other.x) < (node.width + other.width) / 2 + 16
          && Math.abs(node.y - other.y) < (node.height + other.height) / 2 + 16)
          .map(other => [node.id, other.id])),
        satellites: nodes.filter(node => node.area === 'model' && node.outgoing.length === 1).map(node => {
          const target = byId.get(edges.find(edge => edge.source === node.id).target);
          const others = nodes.filter(other => other !== node && other !== target).map(other => distance(node, other));
          return {parent: distance(node, target), others};
        })};
    },
    frame: () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    rememberDocument() { window.modelMapOriginalDocument = document; },
    sameDocument: () => document === window.modelMapOriginalDocument,
  };
  return actions[action]();
}
