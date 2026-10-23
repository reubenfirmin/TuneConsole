/* Session-scoped, slowly evolving contours for the radio landscape. */
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
    syncArtworkSession() {
      const playlistId = this.state.active && this.state.playlist_id;
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
      const running = this.visible && this.state.active && !this.state.starting &&
        !this.destroyed && !document.hidden;
      if (!running) {
        this.pauseArtworkMotion();
        return;
      }
      if (this.artworkFrame !== null) return;
      this.artworkRunning = true;
      let previous = performance.now();
      const frame = now => {
        if (!this.visible || !this.state.active || this.state.starting || this.destroyed || document.hidden) {
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
      const stream = Math.max(.08, Math.min(.92,
        (this.queueCenters[this.connectedSlot] ?? (this.connectedSlot + .5) * 250) / 1000));
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
  };
}
