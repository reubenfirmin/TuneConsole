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
  let echoCache;
  let landscapeCache;
  const junctionCache = new Map();
  // A saved seed reproduces the whole motion, without storing geometry or
  // choosing new random values on every frame.
  const generator = seed => {
    let value = seed;
    return () => {
      value = (value + 0x6D2B79F5) | 0;
      let bits = Math.imul(value ^ value >>> 15, 1 | value);
      bits ^= bits + Math.imul(bits ^ bits >>> 7, 61 | bits);
      return ((bits ^ bits >>> 14) >>> 0) / 4294967296;
    };
  };
  const rhythms = seed => {
    const random = generator(seed);
    return [1, Math.SQRT1_2, 1 / Math.sqrt(5), 1 / Math.sqrt(3), Math.SQRT2]
      .map(rate => ({rate: rate * (.75 + random() * .25), offset: random() * Math.PI * 2}));
  };
  const landscape = seed => {
    const random = generator(seed ^ 0xA511E9B3);
    const signed = (minimum, range) => (random() < .5 ? -1 : 1) * (minimum + random() * range);
    // Local fields choose their own position, scale, travel and oscillator.
    // Folds can form anywhere in the drawing, instead of at three fixed knots.
    return Array.from({length: 3 + Math.floor(random() * 3)}, () => ({
      kind: Math.floor(random() * 4), center: 80 + random() * 1040,
      width: 120 + random() * 200, travel: signed(60, 180),
      horizontal: signed(40, 100), vertical: signed(75, 125), compression: 1.1 + random() * 1.6,
      rate: .45 + random() * 1.1, offset: random() * Math.PI * 2,
      secondary: random() * Math.PI * 2,
    }));
  };
  return {
    artworkPhase: 0, artworkSeed: 0, artworkRhythms: rhythms(0), artworkPlaylistId: null,
    artworkLandscape: landscape(0),
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
      this.artworkLandscape = landscape(this.artworkSeed);
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
        if (elapsed >= 50) {
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
      // Every session begins with ordered bands. Ease in the seeded fields
      // over the first few minutes, then let them fold and travel freely.
      const freedom = 1 - Math.exp(-((phase / .65) ** 2));
      const wave = i => Math.sin(phase * this.artworkRhythms[i].rate + this.artworkRhythms[i].offset);
      if (landscapeCache?.seed !== this.artworkSeed || landscapeCache.phase !== phase) {
        const oscillate = (field, time) => {
          switch (field.kind) {
            case 0: return Math.sin(time);
            case 1: return (Math.sin(time) + .45 * Math.sin(time * Math.SQRT1_2 + field.secondary)) / 1.45;
            case 2: return Math.sin(time) * Math.cos(time / Math.sqrt(3) + field.secondary);
            default: return Math.sin(time + .85 * Math.sin(time / Math.sqrt(5) + field.secondary));
          }
        };
        landscapeCache = {seed: this.artworkSeed, phase, fields: this.artworkLandscape.map(field => {
          const time = phase * field.rate + field.offset;
          return {...field, center: field.center + field.travel * Math.sin(time * Math.SQRT1_2),
            horizontal: freedom * field.horizontal * oscillate(field, time),
            vertical: freedom * field.vertical * oscillate(field, time + field.secondary),
            compression: freedom * field.compression * oscillate(field, time + Math.PI / 2)};
        })};
      }
      const spread = index - 6.5;
      const slot = this.artworkView.connectedSlot;
      const stream = Math.max(.08, Math.min(.92,
        (this.queueCenters[slot] ?? (slot + .5) * 250) / 1000));
      const stretch = Math.max(0, historyHeight + gapHeight * .82 - (300 * scale + offset));
      const drift = (stream - .5) * width * .045;
      // Deform the whole field together as its lower part opens into the junction.
      const warp = ([x, y], tangent = null) => {
        let dx = 0, dy = 0, xx = 1, yx = 0, yy = 1;
        for (const field of landscapeCache.fields) {
          const distance = (x - field.center) / field.width;
          const influence = Math.exp(-distance * distance);
          const slope = -2 * distance / field.width * influence;
          const vertical = field.vertical + (y - 170) * field.compression;
          dx += influence * field.horizontal;
          dy += influence * vertical;
          xx += slope * field.horizontal;
          yx += slope * vertical;
          yy += influence * field.compression;
        }
        const t = Math.max(0, Math.min(1, (y + dy - 180) / 120));
        const ease = t * t * (3 - 2 * t), slope = 6 * t * (1 - t) / 120;
        if (!tangent) return [(x + dx) * scale + drift * ease, (y + dy) * scale + offset + stretch * ease];
        const tx = tangent[0] * xx, ty = tangent[0] * yx + tangent[1] * yy;
        return [tx * scale + drift * slope * ty, ty * (scale + stretch * slope)];
      };
      const reflect = (point, handle) => point.map((value, axis) => 2 * value - handle[axis]);
      const start = [-40, 58 + index * 18];
      const a = [244, 155 + spread * 17], ah = [122, 130 + spread * 17];
      const b = [580, 184 + spread * 13], bh = [440, 213 + spread * 13];
      const c = [886, 155 + spread * 15], ch = [730, 126 + spread * 15];
      const curves = [
        [start, [110, 58 + index * 18], ah, a],
        [a, reflect(a, ah), bh, b],
        [b, reflect(b, bh), ch, c],
        [c, reflect(c, ch), [1070, 195 + spread * 15], [1250, 170 + spread * 15]],
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
      const phase = this.artworkPhase, seed = this.artworkSeed;
      if (echoCache?.width === width && echoCache.height === height &&
          echoCache.phase === phase && echoCache.seed === seed) return echoCache.copies;
      const scale = Math.max(width / 1200, height / 340);
      const primary = {x: 244 * scale, y: height / 2 + scale, r: 119 * scale,
        depth: 0, heading: 0, id: -1, opacity: 1};
      const copies = [], placed = [primary];
      const smooth = value => { const t = Math.max(0, Math.min(1, value)); return t * t * (3 - 2 * t); };
      const wave = (index, offset = 0) => {
        const rhythm = this.artworkRhythms[index % 5];
        return Math.sin(phase * 9 * rhythm.rate + rhythm.offset + offset);
      };
      const project = (parent, heading, ratio, branch, budget, growth = 1) => {
        const depth = parent.depth + 1;
        const r = parent.r * ratio * (.94 + .06 * wave(branch + depth)) * (.6 + .4 * growth);
        const distance = parent.r + r + Math.max(8, 10 * scale);
        const x = parent.x + Math.cos(heading) * distance;
        const y = parent.y + Math.sin(heading) * distance;
        const clearance = Math.min(x - r, width - x - r, y - r, height - y - r,
          ...placed.map(other => Math.hypot(x - other.x, y - other.y) - r - other.r));
        // Fade before a complete disc reaches an edge or another disc. Invisible
        // ancestors cannot leave disconnected descendants floating in the field.
        const opacity = parent.opacity * growth * smooth((clearance - 4) / 10) * smooth((r - 3) / 3);
        const copy = {id: copies.length, parent: parent.id, depth, heading, x, y, r, opacity,
          transform: `translate(${x} ${y}) scale(${r / 119}) translate(-244 -171)`};
        copies.push(copy);
        placed.push(copy);
        if (depth >= 4 || budget <= 1) return;
        // Each projection turns with its parent. Its three children open into
        // a fan as the parent descends, then curl as that parent turns upward.
        const position = (y - primary.y) / primary.r;
        const bend = .24 * Math.sin(heading * 1.7 + position) + .18 * wave(branch + 2, depth);
        const fan = 1.04 + .22 * Math.sin(heading + position + wave(branch + 3));
        if (budget >= 4) {
          const children = [];
          for (const spread of [0, -1, 1]) {
            const start = copies.length;
            project(copy, heading + bend + spread * fan, spread ? .38 : .5, branch, 1);
            children.push(copies[start]);
          }
          // Spend the remaining two slots on the central branch. Its last
          // generation grows only at some parent angles, rather than making
          // a permanent fourth row. Four roots have six slots each: 24 total.
          const child = children[0];
          project(child, child.heading + bend, .52, branch, budget - 4);
        } else {
          const emergence = depth === 3
            ? smooth((Math.sin(heading * 1.3 + position + wave(branch + 1)) + .25) / .9) : 1;
          project(copy, heading + bend, .52, branch, budget - 1, emergence);
        }
      };
      [0, Math.PI, -.9, .9].forEach((angle, branch) => {
        const heading = angle + .24 * wave(branch) + .09 * wave(branch + 2, angle);
        project(primary, heading, [ .46, .4, .31, .31 ][branch], branch, 6);
      });
      echoCache = {width, height, phase, seed, copies};
      return copies;
    },
    orbitTransform(index) {
      const phase = this.artworkPhase * 9;
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
      const fork = slot === this.artworkView.connectedSlot ? this.junctionForkX ?? x : x;
      const {x: cx, y: cy, rx, ry, insetX, insetY} = this.junctionHub;
      const ease = t => (t = Math.max(0, Math.min(1, t))) * t * (3 - 2 * t);
      const key = `${slot}:${detail}`;
      const values = [x, fork, cx, cy, rx, ry, insetX, insetY, this.drawingSpace.width, this.drawingSpace.gapHeight];
      const cached = junctionCache.get(key);
      if (cached?.values.every((value, i) => value === values[i])) return cached.style;
      const finish = path => {
        const style = `d: path("${path}")`;
        // The four streams share their outline across fill, hatch, clip and ink.
        // Cache only their latest geometry, never an animation's entire history.
        if (junctionCache.size >= 8 && !junctionCache.has(key)) junctionCache.clear();
        junctionCache.set(key, {values, style});
        return style;
      };
      const sample = (a, b, c, d, steps) => Array.from({length: steps}, (_, i) => {
        const t = (i + 1) / steps, u = 1 - t;
        return a.map((value, axis) => u ** 3 * value + 3 * u * u * t * b[axis]
          + 3 * u * t * t * c[axis] + t ** 3 * d[axis]);
      });
      // Spread the pull through the whole lower Y, rather than adding a
      // horizontal shoulder at the foot. Route the deformed arms afterwards:
      // the fixed control must stay clear throughout the movement.
      const bend = ([px, py]) => [px + (x - fork) * ease((py - 8) / 104), py];
      // Each arm stays on its own side of the control until it can join the
      // stem BELOW it. This is also the normal, settled route, not a temporary
      // detour used only while switching streams.
      const arm = (start, end, depth, shoulder, radiusX, radiusY) => {
        const side = Math.sign(start - cx), points = [[start, -3], [start, 8]];
        const clearance = 1.002;
        let previous;
        for (const point of sample([start, 8], [start, shoulder], [end, 22], [end, depth], 48).map(bend)) {
          let q = [(point[0] - cx) / radiusX, (point[1] - cy) / radiusY];
          if (Math.abs(q[1]) < clearance)
            q[0] = side * Math.max(side * q[0], Math.sqrt(clearance ** 2 - q[1] ** 2));
          const length = Math.hypot(...q);
          if (length < clearance) q = length < .00001 ? [0, -clearance] : q.map(v => v * clearance / length);
          if (previous) {
            const delta = q.map((v, axis) => v - previous[axis]);
            const norm = delta[0] ** 2 + delta[1] ** 2;
            const t = norm ? Math.max(0, Math.min(1, -(previous[0] * delta[0] + previous[1] * delta[1]) / norm)) : 0;
            if (Math.hypot(previous[0] + t * delta[0], previous[1] + t * delta[1]) < clearance - .001) {
              const a = Math.atan2(previous[1], previous[0]), b = Math.atan2(q[1], q[0]);
              const turn = Math.atan2(Math.sin(b - a), Math.cos(b - a));
              for (let step = 0; step <= 8; step++) {
                const angle = a + turn * step / 8;
                points.push([cx + Math.cos(angle) * radiusX * clearance, cy + Math.sin(angle) * radiusY * clearance]);
              }
            }
          }
          points.push([cx + q[0] * radiusX, cy + q[1] * radiusY]);
          previous = q;
        }
        return points;
      };
      const tail = (points, end, depth) => points.concat(
        sample([end, depth], [end, 104], [end, 104], [end, 112], 12).map(bend));
      const trace = (points, move = 'M') => `${move}${points[0].join(' ')}${this.smoothJunction(points)}`;
      if (detail) {
        const depth = Math.max(82, cy + ry - insetY / 2 + 4);
        const left = tail(arm(52, fork - 6, depth, 36, rx - insetX / 2, ry - insetY / 2), fork - 6, depth);
        const right = tail(arm(948, fork + 6, depth, 36, rx - insetX / 2, ry - insetY / 2), fork + 6, depth);
        const turn = sample([fork - 6, 112], [fork - 6, 120], [fork + 6, 120], [fork + 6, 112], 8).map(bend);
        return finish(trace(left.concat(turn, right.reverse())) + ' Z');
      }
      const outerDepth = Math.max(82, cy + ry + 4), innerDepth = Math.max(68, cy + ry - insetY + 1);
      const left = tail(arm(40, fork - 18, outerDepth, 48, rx, ry), fork - 18, outerDepth);
      const right = tail(arm(960, fork + 18, outerDepth, 48, rx, ry), fork + 18, outerDepth);
      const innerRight = arm(936, fork, innerDepth, 26, rx - insetX, ry - insetY);
      const innerLeft = arm(64, fork, innerDepth, 26, rx - insetX, ry - insetY);
      const path = trace(left) + ` L${x + 18} 112` + trace(right.reverse(), ' L') +
        trace(innerRight.concat(innerLeft.reverse()), ' L') + ' Z';
      return finish(path);
    },
    smoothJunction(points) {
      // Round the routed contour in screen space, including its fork and stem.
      // Uniform knots round the old contour's angular bends; a cubic
      // B-spline shares derivatives across every join and keeps the silhouette.
      const sx = this.drawingSpace.width / 1000, sy = this.drawingSpace.gapHeight / 112;
      const knots = [points[0]];
      let remaining = 6;
      for (let i = 1; i < points.length; i++) {
        let a = points[i - 1];
        const b = points[i];
        let distance = Math.hypot((b[0] - a[0]) * sx, (b[1] - a[1]) * sy);
        while (distance >= remaining) {
          a = a.map((value, axis) => value + (b[axis] - value) * remaining / distance);
          knots.push(a);
          distance -= remaining;
          remaining = 6;
        }
        remaining -= distance;
      }
      const end = points[points.length - 1];
      knots.push(end);
      const p = [knots[0], knots[0], ...knots, end, end];
      let path = '';
      for (let i = 1; i < p.length - 2; i++) {
        const a = p[i].map((v, axis) => (2 * v + p[i + 1][axis]) / 3);
        const b = p[i].map((v, axis) => (v + 2 * p[i + 1][axis]) / 3);
        const c = p[i].map((v, axis) => (v + 4 * p[i + 1][axis] + p[i + 2][axis]) / 6);
        path += ` C${a.join(' ')} ${b.join(' ')} ${c.join(' ')}`;
      }
      return path;
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
    fitSpace() {
      if (!this.artworkView.visible) return;
      const main = this.$refs.fieldContent.closest('main');
      const bottomPadding = main ? parseFloat(getComputedStyle(main).paddingBottom) : 20;
      const session = this.$refs.session;
      if (session?.getClientRects().length) {
        const top = session.getBoundingClientRect().top + window.scrollY;
        const height = Math.max(0, window.innerHeight - top - bottomPadding);
        session.style.setProperty('--radio-session-height', height + 'px');
        const junction = this.$refs.junction.getBoundingClientRect().height;
        const history = Math.max(0, Math.min(height * .52, height - junction - 120,
          Math.max(140, window.innerHeight * .5 - top - 16)));
        session.style.setProperty('--radio-history-height', history + 'px');
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
