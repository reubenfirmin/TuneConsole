/**
 * @typedef {Object} RadioArtworkView
 * @property {string|null} playlistId Drawing identity, independent of a status request.
 * @property {boolean} visible Whether the Radio panel is displayed.
 * @property {boolean} moving Whether the session allows artwork motion.
 * @property {number} connectedSlot Hover/focus preview or the active direction.
 * @property {number} directionCount Number of displayed queue directions.
 *
 * Presentation mixin: the host supplies a readonly artworkView, Alpine $refs
 * and $nextTick. It owns geometry, DOM measurements, motion and saved drawings;
 * it never reads session payloads or starts requests. Compose its property
 * descriptors so computed geometry remains reactive rather than being copied.
 */
function radioArtwork() {
  // A saved seed reproduces the whole motion, without storing geometry or
  // choosing new random values on every frame.
  const rhythms = seed => {
    let value = seed;
    const random = () => {
      value = (value + 0x6D2B79F5) | 0;
      let bits = Math.imul(value ^ value >>> 15, 1 | value);
      bits ^= bits + Math.imul(bits ^ bits >>> 7, 61 | bits);
      return ((bits ^ bits >>> 14) >>> 0) / 4294967296;
    };
    return [1, Math.SQRT1_2, 1 / Math.sqrt(5), 1 / Math.sqrt(3), Math.SQRT2]
      .map(rate => ({rate: rate * (.75 + random() * .25), offset: random() * Math.PI * 2}));
  };
  return {
    artworkPhase: 0, artworkSeed: 0, artworkRhythms: rhythms(0), artworkPlaylistId: null,
    artworkFrame: null, artworkRunning: false,
    queueCenters: {},
    junctionX: null, junctionForkX: null, junctionTarget: null, junctionFrame: null,
    junctionVelocity: 0, junctionForkVelocity: 0,
    junctionHub: {x: 500, y: 56, rx: 45, ry: 40, insetX: 4, insetY: 4},
    drawingSpace: {width: 1200, historyHeight: 340, gapHeight: 112},
    fieldHub: {x: 0, y: 0, r: 0},
    fieldArea: {cx: 0, cy: 0, rx: 1, ry: 1},
    initArtwork() {
      this._artworkChanged = () => this.syncArtworkMotion();
      this._artworkLeaving = () => this.pauseArtworkMotion();
      document.addEventListener('visibilitychange', this._artworkChanged);
      window.addEventListener('pagehide', this._artworkLeaving);
      window.addEventListener('pageshow', this._artworkChanged);
      this.$watch?.('artworkView.connectedSlot', () => this.moveJunction());
    },
    destroyArtwork() {
      this.pauseArtworkMotion();
      cancelAnimationFrame(this.junctionFrame);
      document.removeEventListener('visibilitychange', this._artworkChanged);
      window.removeEventListener('pagehide', this._artworkLeaving);
      window.removeEventListener('pageshow', this._artworkChanged);
    },
    syncArtworkSession() {
      const playlistId = this.artworkView.playlistId;
      if (!playlistId) {
        if (this.artworkPlaylistId) {
          this.pauseArtworkMotion();
          this.artworkPlaylistId = null;
          this.artworkPhase = 0;
          try { sessionStorage.removeItem('radio-artwork'); } catch (_) {}
        }
        return;
      }
      if (playlistId === this.artworkPlaylistId) return;
      this.pauseArtworkMotion();
      let saved;
      try { saved = JSON.parse(sessionStorage.getItem('radio-artwork')); } catch (_) {}
      const resume = saved?.playlistId === playlistId && Number.isFinite(saved.phase) &&
        saved.phase >= 0 && saved.phase <= Number.MAX_SAFE_INTEGER &&
        Number.isInteger(saved.seed) && saved.seed >= 0 && saved.seed < 4294967296;
      this.artworkPlaylistId = playlistId;
      this.artworkPhase = resume ? saved.phase : 0;
      this.artworkSeed = resume ? saved.seed : Math.floor(Math.random() * 4294967296);
      this.artworkRhythms = rhythms(this.artworkSeed);
    },
    pauseArtworkMotion() {
      cancelAnimationFrame(this.artworkFrame);
      this.artworkFrame = null;
      this.artworkRunning = false;
      if (!this.artworkPlaylistId) return;
      try { sessionStorage.setItem('radio-artwork', JSON.stringify({
        playlistId: this.artworkPlaylistId, phase: this.artworkPhase, seed: this.artworkSeed,
      })); } catch (_) {}
    },
    syncArtworkMotion() {
      this.syncArtworkSession();
      const running = this.artworkView.moving && !document.hidden;
      if (!running) {
        this.pauseArtworkMotion();
        return;
      }
      if (this.artworkFrame !== null) return;
      this.artworkRunning = true;
      let previous = performance.now();
      const frame = now => {
        if (!this.artworkView.moving || document.hidden) {
          this.pauseArtworkMotion();
          return;
        }
        const elapsed = now - previous;
        // Small direct steps need no CSS catch-up tween. A stalled frame never
        // advances through time we weren't displaying.
        if (elapsed >= 100) {
          this.artworkPhase += Math.min(200, elapsed) * Math.PI * 2 / 720000;
          previous = now;
        }
        this.artworkFrame = requestAnimationFrame(frame);
      };
      this.artworkFrame = requestAnimationFrame(frame);
    },
    landscapeStyle(index) {
      const {width, historyHeight, gapHeight} = this.drawingSpace;
      const scale = Math.max(width / 1200, historyHeight / 340);
      const offset = (historyHeight - 340 * scale) / 2;
      const phase = this.artworkPhase;
      // Every session begins with ordered bands. Ease in its random rhythms
      // over the first few minutes, then let the fans fold and reverse freely.
      const freedom = 1 - Math.exp(-((phase / .65) ** 2));
      const wave = i => Math.sin(phase * this.artworkRhythms[i].rate + this.artworkRhythms[i].offset);
      const sway = freedom * wave(0), fold = freedom * wave(1), turn = freedom * wave(2);
      const breath = freedom * (1 + wave(3)) / 2;
      const spread = index - 6.5;
      const slot = this.artworkView.connectedSlot;
      const stream = Math.max(.08, Math.min(.92,
        (this.queueCenters[slot] ?? (slot + .5) * 250) / 1000));
      const stretch = Math.max(0, historyHeight + gapHeight * .82 - (300 * scale + offset));
      const drift = (stream - .5) * width * .045;
      // Deform the whole field together as its lower part opens into the junction.
      const warp = ([x, y], tangent = null) => {
        const t = Math.max(0, Math.min(1, (y - 180) / 120));
        const ease = t * t * (3 - 2 * t), slope = 6 * t * (1 - t) / 120;
        return tangent
          ? [tangent[0] * scale + drift * slope * tangent[1], tangent[1] * (scale + stretch * slope)]
          : [x * scale + drift * ease, y * scale + offset + stretch * ease];
      };
      const reflect = (point, handle) => point.map((value, axis) => 2 * value - handle[axis]);
      const start = [-40, 58 + index * 18 + 35 * sway];
      const a = [244 + 150 * sway, 155 + 70 * fold + spread * (17 - 20 * breath)];
      const ah = [122 + 110 * sway, 130 + 100 * turn + spread * (17 + 8 * fold)];
      const b = [580 + 190 * fold, 184 + 100 * turn + spread * (13 - 28 * breath)];
      const bh = [440 + 150 * fold, 213 + 120 * sway + spread * (13 + 16 * turn)];
      const c = [886 - 140 * turn, 155 - 100 * fold + spread * (15 - 30 * breath)];
      const ch = [730 - 130 * turn, 126 - 130 * sway + spread * (15 - 10 * fold)];
      const curves = [
        [start, [110, 58 + index * 18 + 90 * turn], ah, a],
        [a, reflect(a, ah), bh, b],
        [b, reflect(b, bh), ch, c],
        [c, reflect(c, ch), [1070, 195 + spread * (15 - 28 * breath) + 80 * sway],
          [1250, 170 + spread * (15 - 28 * breath) + 70 * fold]],
      ];
      const at = (points, t) => {
        const u = 1 - t;
        const position = [0, 1].map(axis => u ** 3 * points[0][axis] + 3 * u * u * t * points[1][axis]
          + 3 * u * t * t * points[2][axis] + t ** 3 * points[3][axis]);
        const tangent = [0, 1].map(axis => 3 * u * u * (points[1][axis] - points[0][axis])
          + 6 * u * t * (points[2][axis] - points[1][axis]) + 3 * t * t * (points[3][axis] - points[2][axis]));
        return {point: warp(position), tangent: warp(position, tangent)};
      };
      const coordinates = point => point.map(value => Number(value.toFixed(2))).join(' ');
      let path = 'M' + coordinates(warp(start));
      for (const curve of curves) {
        let previous = at(curve, 0);
        for (let step = 1; step <= 4; step++) {
          const next = at(curve, step / 4);
          // Match transformed tangents at every join, including where a fold
          // leaves the table. Matching commands also keep hover morphs smooth.
          const outgoing = previous.point.map((value, axis) => value + previous.tangent[axis] / 12);
          const incoming = next.point.map((value, axis) => value - next.tangent[axis] / 12);
          path += ` C${coordinates(outgoing)} ${coordinates(incoming)} ${coordinates(next.point)}`;
          previous = next;
        }
      }
      const ink = .68 + .32 * freedom * ((1 + wave(4) * Math.cos(index * .26)) / 2) ** 6;
      return `d: path("${path}"); opacity: ${ink}`;
    },
    measureQueues() {
      const content = this.$refs.fieldContent?.getBoundingClientRect();
      if (content?.width && content.height) {
        // A broad radial fade leaves a faint trace of dots at the viewport edges.
        const cx = content.left + content.width / 2, cy = content.top + content.height / 2;
        const feather = Math.max(640, Math.min(900, content.width * .6));
        this.fieldArea = {cx, cy,
          rx: Math.max(content.width / 2 + feather, Math.max(cx, window.innerWidth - cx) * 1.08),
          ry: content.height / 2 + feather};
      }
      const bounds = this.$refs.junction?.getBoundingClientRect();
      if (!bounds?.width) return;
      const centers = {};
      this.$refs.directions?.querySelectorAll('.radio-direction').forEach(queue => {
        const rect = queue.getBoundingClientRect();
        centers[queue.dataset.slot] = (rect.left + rect.width / 2 - bounds.left) / bounds.width * 1000;
      });
      this.queueCenters = centers;
      this.moveJunction();
      const playlist = this.$refs.playlist;
      const headerHeight = playlist?.querySelector('thead')?.getBoundingClientRect().height || 0;
      if (playlist) this.drawingSpace = {
        width: bounds.width,
        historyHeight: Math.max(1, playlist.getBoundingClientRect().height - headerHeight),
        gapHeight: bounds.height,
      };
      const button = this.$refs.tuningControl?.getBoundingClientRect();
      if (button?.width && bounds.height) {
        this.fieldHub = {x: button.left + button.width / 2,
          y: button.top + button.height / 2, r: button.width / 2 + 4};
        this.junctionHub = {
          x: (button.left + button.width / 2 - bounds.left) / bounds.width * 1000,
          y: (button.top + button.height / 2 - bounds.top) / bounds.height * 112,
          rx: (button.width / 2 + 8) / bounds.width * 1000,
          ry: (button.height / 2 + 8) / bounds.height * 112,
          insetX: 4 / bounds.width * 1000, insetY: 4 / bounds.height * 112,
        };
      }
    },
    get engravingTransform() {
      const {width, historyHeight} = this.drawingSpace;
      const scale = Math.max(width / 1200, historyHeight / 340);
      return `translate(0 ${(historyHeight - 340 * scale) / 2}) scale(${scale})`;
    },
    get recordEchoes() {
      const {width, historyHeight: height} = this.drawingSpace;
      const scale = Math.max(width / 1200, height / 340);
      const primary = {x: 244 * scale, y: height / 2 + scale, r: 119 * scale, depth: 0};
      const placed = [primary], echoes = [], pending = [primary];
      const sweep = .2;
      const fitsOrbit = copy => {
        const dx = copy.x - primary.x, dy = copy.y - primary.y;
        const distance = Math.hypot(dx, dy), angle = Math.atan2(dy, dx);
        const angles = [angle - sweep, angle + sweep];
        // Include every horizontal/vertical extremum along the entire orbital
        // arc, so its clearance never depends on the current animation frame.
        for (let quarter = Math.ceil((angle - sweep) / (Math.PI / 2));
             quarter * Math.PI / 2 < angle + sweep; quarter++) angles.push(quarter * Math.PI / 2);
        return angles.every(at => {
          const x = primary.x + Math.cos(at) * distance, y = primary.y + Math.sin(at) * distance;
          return x - copy.r >= 8 && x + copy.r <= width - 8 &&
            y - copy.r >= 8 && y + copy.r <= height - 8;
        });
      };
      // Reuse the same disc at smaller scales. Only place complete, separated
      // copies in the measured history area; cap both recursion and SVG work.
      while (pending.length && echoes.length < 24) {
        const parent = pending.shift();
        if (parent.depth >= 4) continue;
        for (const angle of [0, -.7, .7, Math.PI]) {
          // The main disc sits near the left edge. Its left-hand copy needs
          // extra clearance for the entire sweep on narrow history panels.
          const r = parent.r * (angle === 0 ? .46 : angle === Math.PI ? .4 : .31);
          if (r < 14) continue;
          const distance = parent.r + r + 10;
          const copy = {x: parent.x + Math.cos(angle) * distance,
            y: parent.y + Math.sin(angle) * distance, r, depth: parent.depth + 1};
          if (!fitsOrbit(copy) ||
              placed.some(other => Math.hypot(copy.x - other.x, copy.y - other.y) < r + other.r + 8)) continue;
          placed.push(copy);
          echoes.push({...copy, id: echoes.length});
          pending.push(copy);
          if (echoes.length === 24) break;
        }
      }
      // Move the constellation together around the main record. A bounded
      // sweep fits even a shallow phone viewport, preserves every separation,
      // and leaves each cover upright. Its phase already survives navigation
      // and pauses while the page is hidden.
      const turn = sweep * Math.sin(this.artworkPhase * this.artworkRhythms[0].rate * .7);
      return echoes.map(copy => {
        const dx = copy.x - primary.x, dy = copy.y - primary.y;
        const x = primary.x + dx * Math.cos(turn) - dy * Math.sin(turn);
        const y = primary.y + dx * Math.sin(turn) + dy * Math.cos(turn);
        return {...copy, x, y,
          transform: `translate(${x} ${y}) scale(${copy.r / 119}) translate(-244 -171)`};
      });
    },
    orbitTransform(index) {
      const phase = this.artworkPhase;
      const rhythm = this.artworkRhythms[index + 1];
      const sway = Math.sin(phase * rhythm.rate + rhythm.offset) - Math.sin(rhythm.offset);
      const tilt = (index ? 28 : -19) + sway * (index ? 34 : 26);
      const flatten = 1 + .18 * sway;
      return `translate(${244 + sway * 9} ${171 + sway * (index ? -7 : 7)}) ` +
        `rotate(${tilt}) scale(1 ${flatten}) translate(-244 -171)`;
    },
    get landscapeToJunction() {
      const {width, historyHeight, gapHeight} = this.drawingSpace;
      return `matrix(${1000 / width} 0 0 ${112 / gapHeight} 0 ${-historyHeight * 112 / gapHeight})`;
    },
    get drawingClearance() {
      const {width, historyHeight, gapHeight} = this.drawingSpace;
      const {x, y, rx, ry, insetX, insetY} = this.junctionHub;
      const cx = x * width / 1000, cy = historyHeight + y * gapHeight / 112;
      const w = (rx - insetX) * width / 1000, h = (ry - insetY) * gapHeight / 112;
      return `M0 0H${width}V${historyHeight + gapHeight}H0Z M${cx - w} ${cy} ` +
        `a${w} ${h} 0 1 0 ${w * 2} 0 a${w} ${h} 0 1 0 ${-w * 2} 0Z`;
    },
    moveJunction() {
      const slot = this.artworkView.connectedSlot;
      const target = this.queueCenters[slot] ?? (slot + .5) * 1000 / this.artworkView.directionCount;
      this.junctionTarget = target;
      if (this.junctionX === null) {
        this.junctionX = this.junctionForkX = target;
        return;
      }
      if (this.junctionFrame !== null || (Math.abs(this.junctionX - target) < .001
          && Math.abs(this.junctionForkX - target) < .001)) return;
      let previous = performance.now();
      const frame = now => {
        // The stem leads and the shoulders follow, like a ribbon pulled sideways.
        // Keep momentum on a reversal, and integrate small steps after a slow frame.
        let elapsed = Math.min(64, now - previous) / 1000;
        previous = now;
        while (elapsed > 0) {
          const dt = Math.min(elapsed, 1 / 120);
          this.junctionVelocity += ((this.junctionTarget - this.junctionX) * 180
            - this.junctionVelocity * 24) * dt;
          this.junctionX += this.junctionVelocity * dt;
          this.junctionForkVelocity += ((this.junctionX - this.junctionForkX) * 140
            - this.junctionForkVelocity * 21) * dt;
          this.junctionForkX += this.junctionForkVelocity * dt;
          elapsed -= dt;
        }
        if (Math.abs(this.junctionForkX - this.junctionTarget) < .02
            && Math.abs(this.junctionX - this.junctionTarget) < .02
            && Math.abs(this.junctionVelocity) + Math.abs(this.junctionForkVelocity) < .1) {
          this.junctionX = this.junctionForkX = this.junctionTarget;
          this.junctionVelocity = this.junctionForkVelocity = 0;
          this.junctionFrame = null;
        } else this.junctionFrame = requestAnimationFrame(frame);
      };
      this.junctionFrame = requestAnimationFrame(frame);
    },
    junctionStyle(slot, detail = false) {
      const target = this.queueCenters[slot] ?? (slot + .5) * 1000 / this.artworkView.directionCount;
      const x = slot === this.artworkView.connectedSlot ? this.junctionX ?? target : target;
      // Keep both table attachments fixed as the fork moves between streams.
      const left = 40, right = 960;
      const {x: cx, y: cy, rx, ry, insetX, insetY} = this.junctionHub;
      const half = 18;
      const fork = slot === this.artworkView.connectedSlot ? this.junctionForkX ?? x : x;
      const midpoint = (a, b) => a.map((value, i) => (value + b[i]) / 2);
      const split = points => {
        const a = midpoint(points[0], points[1]), b = midpoint(points[1], points[2]);
        const c = midpoint(points[2], points[3]), d = midpoint(a, b), e = midpoint(b, c);
        const center = midpoint(d, e);
        return [[points[0], a, d, center], [center, e, c, points[3]]];
      };
      const arm = (start, end, depth, shoulder, radiusX, radiusY) => {
        const points = [[start, 8], [start, shoulder], [end, 22], [end, depth]];
        const segments = split(points).flatMap(split);
        return {points: [points[0], ...segments.flatMap(segment => segment.slice(1))],
          side: Math.sign(start - cx), radiusX, radiusY};
      };
      const trace = (route, reverse = false, move = 'M') => {
        const {points, side, radiusX, radiusY} = route;
        const p = reverse ? [...points].reverse() : points;
        let path = reverse ? `${move}${p[0].join(' ')}` : `${move}${p[0][0]} -3 L${p[0].join(' ')}`;
        for (let i = 1; i < p.length; i += 3) path += ` C${p.slice(i, i + 3).flat().join(' ')}`;
        if (reverse) path += ` L${p[p.length - 1][0]} -3`;
        return this.bendJunction(path, cx, cy, radiusX, radiusY, side);
      };
      // Each shoulder stays on its own side of the control and joins below it.
      // That continuous route cannot flip sides when the moving fork crosses
      // the control's center, including during a rapid reversal.
      let path;
      if (detail) {
        const depth = Math.max(82, cy + ry - insetY / 2 + 4);
        const flowLeft = arm(left + 12, fork - 6, depth, 36, rx - insetX / 2, ry - insetY / 2);
        const flowRight = arm(right - 12, fork + 6, depth, 36, rx - insetX / 2, ry - insetY / 2);
        // One circuit: descend through the left arm and return up the right.
        // Both turnarounds sit just outside the viewport, keeping the visible
        // dots inside the Y while preserving a continuous route during previews.
        path = trace(flowLeft) + ` C${fork - 6} 104 ${x - 6} 104 ${x - 6} 112 ` +
          `C${x - 6} 120 ${x + 6} 120 ${x + 6} 112 ` +
          `C${x + 6} 104 ${fork + 6} 104 ${fork + 6} ${depth} ` + trace(flowRight, true, 'L') + ' Z';
      } else {
        const outerDepth = Math.max(82, cy + ry + 4);
        const innerDepth = Math.max(68, cy + ry - insetY + 1);
        const outerLeft = arm(left, fork - half, outerDepth, 48, rx, ry);
        const outerRight = arm(right, fork + half, outerDepth, 48, rx, ry);
        const innerLeft = arm(left + 24, fork, innerDepth, 26, rx - insetX, ry - insetY);
        const innerRight = arm(right - 24, fork, innerDepth, 26, rx - insetX, ry - insetY);
        path = trace(outerLeft) + ` C${fork - half} 104 ${x - half} 104 ${x - half} 112 L${x + half} 112 ` +
          `C${x + half} 104 ${fork + half} 104 ${fork + half} ${outerDepth} ` + trace(outerRight, true, 'L') + ' ' +
          trace(innerRight, false, 'L') + ' ' + trace(innerLeft, true, 'L') + ' Z';
      }
      return `d: path("${path}")`;
    },
    bendJunction(path, cx, cy, rx, ry, side) {
      const tokens = path.match(/[MLCZ]|-?\d+(?:\.\d+)?(?:e[-+]?\d+)?/gi);
      let i = 0, point, origin, previous, output = '';
      const coordinates = p => `${(cx + p[0] * rx).toFixed(3)} ${(cy + p[1] * ry).toFixed(3)}`;
      const append = (p, move = false) => {
        let q = [(p[0] - cx) / rx, (p[1] - cy) / ry];
        if (Math.abs(q[1]) < 1.002) {
          const edge = Math.sqrt(1.002 ** 2 - q[1] ** 2);
          q[0] = side * Math.max(side * q[0], edge);
        }
        const length = Math.hypot(...q);
        if (length < 1.002) q = length < .00001 ? [0, -1.002] : q.map(v => v * 1.002 / length);
        if (!move && previous) {
          const delta = q.map((v, axis) => v - previous[axis]);
          const norm = delta[0] ** 2 + delta[1] ** 2;
          const t = norm ? Math.max(0, Math.min(1, -(previous[0] * delta[0] + previous[1] * delta[1]) / norm)) : 0;
          if (Math.hypot(previous[0] + t * delta[0], previous[1] + t * delta[1]) < 1.001) {
            const a = Math.atan2(previous[1], previous[0]), b = Math.atan2(q[1], q[0]);
            const turn = Math.atan2(Math.sin(b - a), Math.cos(b - a));
            const entry = [Math.cos(a) * 1.002, Math.sin(a) * 1.002];
            const exit = [Math.cos(b) * 1.002, Math.sin(b) * 1.002];
            output += ` L${coordinates(entry)} A${rx * 1.002} ${ry * 1.002} 0 0 ${turn > 0 ? 1 : 0} ${coordinates(exit)}`;
          }
        }
        output += `${move ? ' ' + path[0] : ' L'}${coordinates(q)}`;
        previous = q;
      };
      while (i < tokens.length) {
        const command = tokens[i++];
        if (command === 'Z') { append(origin); output += ' Z'; continue; }
        const next = () => [+tokens[i++], +tokens[i++]];
        if (command === 'M' || command === 'L') {
          point = next();
          if (command === 'M') origin = point;
          append(point, command === 'M');
        } else if (command === 'C') {
          const start = point, a = next(), b = next(), end = next();
          for (let sample = 1; sample <= 12; sample++) {
            const t = sample / 12, u = 1 - t;
            append([0, 1].map(axis => u ** 3 * start[axis] + 3 * u ** 2 * t * a[axis]
              + 3 * u * t ** 2 * b[axis] + t ** 3 * end[axis]));
          }
          point = end;
        }
      }
      return output.trim();
    },
    get junctionClearance() {
      const {x, y, rx, ry, insetX, insetY} = this.junctionHub;
      const w = rx - insetX * 1.5, h = ry - insetY * 1.5;
      // Keep the unfilled control clear during intermediate hover frames too.
      return `M0 0H1000V112H0Z M${x - w} ${y} a${w} ${h} 0 1 0 ${w * 2} 0 a${w} ${h} 0 1 0 ${-w * 2} 0Z`;
    },
    layoutHistory() {
      const scroller = this.$refs.playlist?.querySelector('.radio-track-scroll');
      const space = scroller?.querySelector('.radio-history-space');
      const history = scroller?.querySelector('.radio-history');
      if (!space || !history || !scroller.clientHeight) return;
      const headerHeight = scroller.querySelector('thead')?.getBoundingClientRect().height || 0;
      const rowHeight = history.querySelector('tr')?.getBoundingClientRect().height || 48;
      const height = Math.max(0, scroller.clientHeight - headerHeight - history.getBoundingClientRect().height);
      this.$refs.playlist.style.setProperty('--radio-table-head', headerHeight + 'px');
      const layoutKey = [scroller.clientWidth, scroller.clientHeight, height, history.lastElementChild?.dataset.vid].join(':');
      if (space.dataset.layout === layoutKey) return;
      space.dataset.layout = layoutKey;
      const count = Math.ceil(height / rowHeight);
      space.replaceChildren(...Array.from({length: count}, () => {
        const row = document.createElement('tr');
        row.className = 'radio-empty-row';
        row.style.height = height / count + 'px';
        const cell = row.insertCell();
        cell.colSpan = scroller.querySelector('thead tr')?.children.length || 10;
        return row;
      }));
      // Keep the newest played row at the junction, including after a table swap.
      scroller.scrollTop = scroller.scrollHeight;
    },
    launchPosition(index) {
      const angle = index * Math.PI / 4;
      return {x: 50 + 38 * Math.cos(angle), y: 50 + 34 * Math.sin(angle)};
    },
    fitSpace() {
      if (!this.artworkView.visible) return;
      const main = this.$refs.fieldContent.closest('main');
      const bottomPadding = main ? parseFloat(getComputedStyle(main).paddingBottom) : 20;
      for (const [element, variable] of [[this.$refs.seeds, '--radio-grid-height'],
                                        [this.$refs.launch, '--radio-launch-height'],
                                        [this.$refs.session, '--radio-session-height']]) {
        if (!element?.getClientRects().length) continue;
        const top = element.getBoundingClientRect().top + window.scrollY;
        const height = Math.max(0, window.innerHeight - top - bottomPadding);
        element.style.setProperty(variable, height + 'px');
        if (element === this.$refs.session) {
          const junction = this.$refs.junction.getBoundingClientRect().height;
          const history = Math.max(0, Math.min(height * .52, height - junction - 120,
            Math.max(140, window.innerHeight * .5 - top - 16)));
          element.style.setProperty('--radio-history-height', history + 'px');
        }
      }
      this.layoutHistory();
      this.measureQueues();
    },
    animateQueues(slots) {
      if (!slots.length) return;
      this.$nextTick(() => {
        for (const slot of slots) {
          const card = this.$refs.directions?.querySelector(`[data-slot="${slot}"]`);
          for (const element of card?.querySelectorAll('.radio-queue-art, .radio-direction-tracks') || [])
            element.animate([{opacity: .15, filter: 'blur(4px)', transform: 'translateY(8px)'},
              {opacity: 1, filter: 'blur(0)', transform: 'translateY(0)'}], {duration: 650, easing: 'cubic-bezier(.2,.8,.2,1)'});
        }
      });
    },
  };
}
