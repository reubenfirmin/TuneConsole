/* One loader for saved playlists and On Air. Own every frame, observer and timer. */
function createLaunchLoader(stage, initial = {}) {
  const visual = stage.querySelector('.gen-visual'), orbit = stage.querySelector('.gen-orbit');
  const canvas = stage.querySelector('canvas'), ctx = canvas.getContext('2d');
  const copy = stage.querySelector('.gen-copy'), status = stage.querySelector('[data-loader-status]');
  const detail = stage.querySelector('[data-loader-detail]');
  let nodes = [], artKey = null, statusKey = '', statuses = [], statusIndex = 0;
  let width = 0, height = 0, frameId = null, timer = null, active = false, destroyed = false;
  let elapsed = 0, lastTime = null;
  const colors = window.themePalette({accent: '--immersive-accent', trunk: '--trunk',
    edgeHub: '--gen-edge-hub', edgeRim: '--gen-edge-rim', pulseGlow: '--gen-pulse-glow'});

  function resize() {
    stage.style.setProperty('--loader-copy-height', copy.offsetHeight + 'px');
    width = visual.clientWidth; height = visual.clientHeight;
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const nextWidth = Math.round(width * dpr), nextHeight = Math.round(height * dpr);
    if (canvas.width !== nextWidth || canvas.height !== nextHeight) {
      canvas.width = nextWidth; canvas.height = nextHeight;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
  }
  const observer = new ResizeObserver(resize);
  observer.observe(stage); observer.observe(visual); observer.observe(copy);

  function setArt(thumbs, slots) {
    const urls = [...new Set(thumbs.filter(url => typeof url === 'string' && url))].slice(0, nodes.length || 14);
    const key = JSON.stringify(urls);
    if (key === artKey) return;
    artKey = key;
    const count = Math.max(1, Math.min(14, slots || urls.length || 9));
    if (!nodes.length) nodes = Array.from({length: count}, (_, i) => {
      const el = document.createElement('div');
      el.className = 'gen-node';
      orbit.append(el);
      // Distribute radii across the graph, with separate phase and drift for every thumbnail.
      return {el, angle: i / count * Math.PI * 2, radius: .53 + ((i * .618) % 1) * .4,
        speed: (.04 + (i % 4) * .009) * (i % 2 ? -1 : 1), phase: i * 2.399};
    });
    // Keep each orbital node and its motion parameters. Matching artwork stays
    // in its slot; only vacated slots receive newly established tracks.
    const existing = new Set(nodes.map(node => node.url).filter(url => urls.includes(url)));
    const replacements = urls.filter(url => !existing.has(url));
    for (const node of nodes) {
      const url = urls.includes(node.url) ? node.url : replacements.shift();
      if (url === node.url && node.el.childElementCount) continue;
      node.url = url;
      node.el.replaceChildren();
      node.el.classList.toggle('no-art', !url);
      if (!url) continue;
      const img = document.createElement('img');
      img.alt = ''; img.referrerPolicy = 'no-referrer'; img.src = url;
      img.onerror = () => {
        if (node.url !== url || !img.isConnected) return;
        node.el.classList.add('no-art'); img.remove();
      };
      node.el.append(img);
    }
  }
  function frame(time) {
    frameId = null;
    if (!active || document.hidden || destroyed) return;
    if (lastTime !== null) elapsed += Math.min(time - lastTime, 100);
    lastTime = time;
    const t = elapsed / 1000, cx = width / 2, cy = height / 2;
    const rx = Math.max(0, Math.min(width * .42, height * .75, 380) - 28);
    const ry = Math.max(0, Math.min(height * .43, rx * .7) - 16);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = colors.accent; ctx.globalAlpha = .1;
    for (let x = cx % 36; x < width; x += 36) {
      for (let y = cy % 36; y < height; y += 36) ctx.fillRect(x, y, 1.4, 1.4);
    }
    ctx.globalAlpha = 1;
    for (const node of nodes) {
      const angle = node.angle + t * node.speed + .09 * Math.sin(t * .23 + node.phase);
      const r = node.radius * (1 + .045 * Math.sin(t * .6 + node.phase));
      const x = cx + rx * r * Math.cos(angle), y = cy + ry * r * Math.sin(angle);
      const depth = (Math.sin(angle) + 1) / 2;
      const gradient = ctx.createLinearGradient(cx, cy, x, y);
      gradient.addColorStop(0, colors.edgeHub); gradient.addColorStop(1, colors.edgeRim);
      ctx.strokeStyle = gradient; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y); ctx.stroke();
      const f = (t * .25 + node.phase) % 1;
      ctx.fillStyle = colors.trunk; ctx.shadowColor = colors.pulseGlow; ctx.shadowBlur = 8;
      ctx.beginPath(); ctx.arc(x + (cx - x) * f, y + (cy - y) * f, 2, 0, Math.PI * 2);
      ctx.fill(); ctx.shadowBlur = 0;
      node.el.style.transform = `translate(${x.toFixed(1)}px,${y.toFixed(1)}px) translate(-50%,-50%) scale(${.78 + .25 * depth})`;
      node.el.style.opacity = .55 + .45 * depth;
    }
    frameId = requestAnimationFrame(frame);
  }
  function sync() {
    cancelAnimationFrame(frameId); frameId = null; lastTime = null;
    clearInterval(timer); timer = null;
    stage.classList.toggle('is-loader-paused', !active || document.hidden);
    if (!active || document.hidden || destroyed) return;
    resize(); frameId = requestAnimationFrame(frame);
    if (statuses.length > 1) timer = setInterval(() => {
      status.textContent = statuses[++statusIndex % statuses.length];
    }, 3500);
  }
  document.addEventListener('visibilitychange', sync);
  function update(options = {}) {
    if (destroyed) return;
    setArt(options.thumbs || [], options.slots);
    const nextStatuses = options.statuses || [status.textContent];
    const key = JSON.stringify(nextStatuses);
    const changed = key !== statusKey || active !== (options.active !== false);
    if (key !== statusKey) {
      statuses = nextStatuses; statusKey = key; statusIndex = 0;
      status.textContent = statuses[0] || 'Getting your music ready';
    }
    detail.textContent = options.detail || '';
    detail.hidden = !options.detail;
    active = options.active !== false;
    if (changed) sync();
  }
  update(initial);
  return {update, destroy() {
    destroyed = true; active = false; sync(); observer.disconnect();
    document.removeEventListener('visibilitychange', sync);
  }};
}

function launchLoaderView(slots = 9) {
  let loader;
  return {
    init() { loader = createLaunchLoader(this.$el, {active: false, slots}); },
    update(options) { loader?.update(options); },
    destroy() { loader?.destroy(); },
  };
}
