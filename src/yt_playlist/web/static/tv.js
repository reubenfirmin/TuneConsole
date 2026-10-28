/* Embedded TV outlives page content until another player takes over. */
let tvApiPromise;
function loadTvApi() {
  if (window.YT?.Player) return Promise.resolve(window.YT);
  if (tvApiPromise) return tvApiPromise;
  tvApiPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    const previous = window.onYouTubeIframeAPIReady;
    let timer = setTimeout(() => fail(), 12000);
    function fail() { clearTimeout(timer); script.remove(); tvApiPromise = null; reject(new Error('player')); }
    window.onYouTubeIframeAPIReady = () => { clearTimeout(timer); previous?.(); resolve(window.YT); };
    script.src = 'https://www.youtube.com/iframe_api'; script.onerror = fail;
    document.head.append(script);
  });
  return tvApiPromise;
}

function tvHome(maximum = 15) {
  let player = null, timer = null, artTimer = null, progressTimer = null, serial = 0, playerSerial = 0, selectionSerial = 0, controller = null;
  let reports = Promise.resolve(), reportedPlay = false, lastTick = 0;
  let refillTimer = null, refillSerial = 0, refillRequest = null, entry = 0;
  let ownsPlayback = false, playbackSerial = 0, loadedVideoId = null, releasePending = false, lastPlayerState = null;
  let startedSelection = null;
  let playbackRetries = 0, retryPending = null;
  let visualRequest = null, visualTimer = null, visualSerial = 0, visualTarget = null;
  const visualChecked = new Set();
  let viewObserver = null, syncView = null, updateView = null, layoutView = null;
  const lengths = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 45, 60, 90, 120, 180, 240, 360, 480, 720, null];
  return {
    items: [], selected: -1, maxMinutes: maximum, lengths, draft: lengths.indexOf(maximum), visible: false,
    ready: false, loading: false, starting: false, connecting: false, playing: false, playerMounted: false,
    playlistId: null, playlistTitle: '', error: '', playerError: '', finished: false,
    stream: '', streamPlaylistId: null, refilling: false, waiting: false, refillError: '', refillAttempts: 0,
    failedVideos: [],
    destroyed: false, failed: [], revealedCount: 0, sessionStarted: false, savedPlaylistOpened: null,
    failedArt: [],
    get generating() { return this.starting || this.connecting || (!this.ready && !this.playerMounted && !this.error && !this.playerError); },
    get current() { return this.items[this.selected]; },
    get lengthLabel() {
      const minutes = this.lengths[this.draft];
      return minutes === null ? 'Unlimited' : minutes < 60 ? minutes + ' min' :
        Math.floor(minutes / 60) + 'h' + (minutes % 60 ? ' ' + minutes % 60 + 'm' : '');
    },
    get revealed() { return this.items.slice(0, this.revealedCount); },
    get backdrop() {
      const candidates = this.current?.artist_art || [];
      const image = candidates.find(image => !this.failedArt.includes(image));
      if (image) return [image];
      return !this.current?.artist_art_pending && !this.failedArt.includes(this.current?.thumbnail)
        ? [this.current?.thumbnail].filter(Boolean) : [];
    },
    artFailed(image) { this.failedArt = [...this.failedArt.filter(url => url !== image), image].slice(-60); },
    async refreshArtwork() {
      clearTimeout(artTimer);
      const video = this.current;
      if (!video || !this.visible || this.destroyed) return;
      const intent = playbackSerial;
      try {
        const response = await fetch('/home/tv/artwork?' + new URLSearchParams({artist: video.source_artist || video.artist}));
        const data = await response.json();
        if (intent !== playbackSerial || this.current !== video || !this.visible || this.destroyed) return;
        if (response.ok) { video.artist_art = data.images; video.artist_art_pending = data.pending; }
        if (data.pending) artTimer = setTimeout(() => this.refreshArtwork(), 5000);
      } catch (_) {
        if (intent === playbackSerial && this.current === video && this.visible && !this.destroyed)
          artTimer = setTimeout(() => this.refreshArtwork(), 10000);
      }
    },
    init() {
      const owner = this;
      this.controls = {active: () => ownsPlayback && !!player,
        keepAlive: () => owner.playerMounted || owner.starting || owner.connecting,
        command(action) {
          if (action === 'next') owner.next();
          else if (action === 'pause' || (action === 'toggle' && owner.playing)) player?.pauseVideo();
          else owner.resume();
          return true;
        },
        rated(videoId, status) {
          const item = owner.items.find(item => item.video_id === videoId);
          if (item) item.likeStatus = status;
          if (status === 'DISLIKE' && owner.current?.video_id === videoId) owner.next();
          owner.publish();
        },
        release: () => owner.externalPlayback(),
        snapshot: () => ({...owner.current, video_id: owner.current?.video_id, paused: !owner.playing,
          position: player?.getCurrentTime?.() ?? null,
          duration: player?.getDuration?.() || owner.current?.duration,
          source: {kind: 'tv', title: owner.playlistTitle, url: '/#tv', external: false}})};
      window.tvPlaybackControls = this.controls;
      // Reparenting an iframe reloads it. Keep the entire TV component mounted
      // here, and align it with Home's replaceable placeholder instead.
      layoutView = () => {
        const slot = document.querySelector('.tv-slot');
        if (!this.visible || !slot) return;
        const box = slot.getBoundingClientRect();
        Object.assign(this.$el.style, {left: box.left + scrollX + 'px', top: box.top + scrollY + 'px', width: box.width + 'px'});
        const height = this.$el.offsetHeight + 'px';
        if (slot.style.height !== height) slot.style.height = height;
      };
      syncView = () => {
        const slot = document.querySelector('.tv-slot');
        const dashboard = slot?.closest('.home-dashboard');
        this.setVisible(!!dashboard && Alpine.$data(dashboard).panel === 'tv');
        viewObserver.disconnect();
        viewObserver.observe(this.$el);
        viewObserver.observe(document.body);
        if (slot) viewObserver.observe(slot);
        const main = document.getElementById('app-page');
        if (main) viewObserver.observe(main);
        layoutView();
      };
      viewObserver = new ResizeObserver(layoutView);
      updateView = () => this.$nextTick(syncView);
      window.addEventListener('app-page-changed', updateView);
      window.addEventListener('hashchange', updateView);
      window.addEventListener('resize', layoutView);
      window.addEventListener('animationend', layoutView);
      this.$nextTick(syncView);
    },
    setVisible(value) {
      if (value) {
        const saved = new URLSearchParams(location.search).get('tv_playlist');
        if (saved && saved !== this.savedPlaylistOpened) {
          this.savedPlaylistOpened = saved;
          this.openPlaylist(saved);
        } else if (!saved) this.savedPlaylistOpened = null;
      }
      if (value === this.visible) return;
      this.visible = value;
      if (value) {
        if (!this.playerMounted && !this.starting && !this.loading) this.readiness();
        if (this.current?.artist_art_pending) this.refreshArtwork();
        this.revealCurrent();
      } else {
        clearTimeout(artTimer);
      }
      this.publish();
    },
    publish() {
      document.body.classList.toggle('tv-is-playing', this.visible && this.playing);
      window.dispatchEvent(new Event('tv-playback-state'));
    },
    revealCurrent() {
      if (!this.visible) return;
      this.$nextTick(() => {
        const strip = this.$refs.filmstrip, current = strip?.querySelector('[aria-current="true"]');
        if (!current) return;
        const left = current.offsetLeft - strip.offsetLeft;
        const ahead = current.nextElementSibling || current;
        const right = ahead.offsetLeft - strip.offsetLeft + ahead.offsetWidth;
        if (left < strip.scrollLeft) strip.scrollLeft = left;
        else if (right > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = right - strip.clientWidth;
      });
    },
    report(kind, state = '') {
      if (!this.playlistId || !this.current) return;
      const body = new URLSearchParams({kind, state, video_id: this.current.video_id,
        position: player?.getCurrentTime?.() || 0,
        time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone || ''});
      const url = '/home/tv/playlist/' + (this.current.playlist_id || this.playlistId) + '/event';
      reports = reports.catch(() => {}).then(() => fetch(url, {method: 'POST', body, keepalive: true}));
    },
    reportPlay() {
      if (!reportedPlay && ownsPlayback && this.playing && !player?.isMuted?.() && player?.getVolume?.() !== 0) {
        reportedPlay = true; this.report('play');
      }
    },
    async readiness() {
      const request = ++serial;
      controller?.abort(); controller = new AbortController(); clearTimeout(timer);
      this.loading = true;
      try {
        const response = await fetch('/home/tv', {signal: controller.signal});
        const data = await response.json();
        if (request !== serial || this.destroyed) return;
        if (!response.ok) throw new Error(data.error || 'Couldn’t load TV. Try again.');
        this.ready = data.ready; this.maxMinutes = data.max_minutes;
        this.error = data.message || '';
        if (!this.ready && this.visible) timer = setTimeout(() => this.readiness(), 5000);
      } catch (error) {
        if (request === serial && !this.destroyed && error.name !== 'AbortError') {
          this.error = 'Couldn’t load TV. Try again.';
        }
      } finally { if (request === serial) this.loading = false; }
    },
    async start() {
      if (this.starting) return;
      this.stopPlayer();
      const intent = playbackSerial;
      this.starting = true; this.sessionStarted = false; this.error = ''; this.playerError = '';
      return this.generate(intent);
    },
    async generate(intent) {
      if (intent !== playbackSerial || this.destroyed) return;
      try {
        const response = await fetch('/home/tv/play', {method: 'POST'});
        const data = await response.json();
        if (intent !== playbackSerial || this.destroyed) return;
        if (response.status === 202) {
          if (data.message) { this.starting = false; this.error = data.message; return; }
          // An empty or rebuilding pool is normal preparation. Keep the same
          // launch intent and embedded loader until a prepared draw is ready.
          timer = setTimeout(() => this.generate(intent), 2000);
          return;
        }
        if (!response.ok) throw new Error(data.error || 'Couldn’t start TV. Try again.');
        this.ready = true;
        this.usePlaylist(data);
        await this.select(0);
      } catch (error) {
        if (intent === playbackSerial && !this.destroyed) {
          this.starting = false;
          this.error = error instanceof TypeError ? 'Couldn’t start TV. Try again.' : error.message;
        }
      }
    },
    async openPlaylist(id) {
      const intent = playbackSerial;
      this.loading = true; this.connecting = true;
      try {
        const response = await fetch('/home/tv/playlist/' + encodeURIComponent(id));
        const data = await response.json();
        if (intent !== playbackSerial || this.destroyed) return;
        if (!response.ok) throw new Error(data.error);
        this.usePlaylist(data); await this.select(0);
      } catch (error) {
        if (intent !== playbackSerial || this.destroyed) return;
        this.connecting = false;
        this.error = error.message || 'Couldn’t open this video playlist.';
      } finally { if (intent === playbackSerial) this.loading = false; }
    },
    usePlaylist(data) {
      if (!data.items?.length) throw new Error('This video playlist has no available videos. Try another playlist.');
      this.stopPlayer();
      this.items = data.items.map(item => ({...item, queue_id: ++entry,
        playlist_id: data.playlist_id, playlist_title: data.title}));
      this.playlistId = data.playlist_id; this.streamPlaylistId = data.playlist_id; this.stream = data.stream || '';
      this.playlistTitle = data.title; this.selected = -1; this.failed = []; this.finished = false;
      this.failedVideos = []; this.refillError = ''; this.refillAttempts = 0; this.waiting = false;
      this.revealedCount = 0; this.sessionStarted = false;
      this.connecting = true;
    },
    async acquire() {
      const response = await fetch('/home/tv/takeover', {method: 'POST'});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'Couldn’t pause the audio player. Try again.');
    },
    cancelVisualCheck() {
      ++visualSerial; clearTimeout(visualTimer); visualRequest?.abort();
      visualRequest = null; visualTarget = null;
    },
    async inspectAhead(retries = 0) {
      if (this.destroyed || !ownsPlayback || !this.playing) return;
      const index = this.selected + 2, item = this.items[index];
      // Current and next are already visible. Never inspect a visited/revealed
      // entry after the user goes backwards through the filmstrip.
      if (!item || index < this.revealedCount || visualChecked.has(item.video_id)
          || visualTarget === item.queue_id) return;
      this.cancelVisualCheck();
      const attempt = visualSerial, intent = playbackSerial;
      visualTarget = item.queue_id;
      visualRequest = new AbortController();
      const request = visualRequest;
      const timeout = setTimeout(() => request.abort(), 20000);
      const current = () => attempt === visualSerial && intent === playbackSerial
        && !this.destroyed && ownsPlayback && this.playing && this.items[this.selected + 2] === item
        && this.selected + 2 >= this.revealedCount;
      try {
        const response = await fetch('/home/tv/playlist/' + (item.playlist_id || this.playlistId) + '/visual-check',
          {method: 'POST', signal: request.signal, body: new URLSearchParams({video_id: item.video_id})});
        const data = await response.json();
        if (!current()) return;
        if (response.status === 202) {
          if (retries < 3) visualTimer = setTimeout(() => this.inspectAhead(retries + 1), 2000);
          return;
        }
        if (!response.ok || data.video_id !== item.video_id || !['static', 'moving', 'unknown'].includes(data.verdict)) return;
        visualChecked.add(item.video_id);
        if (visualChecked.size > 240) visualChecked.delete(visualChecked.values().next().value);
        if (data.verdict === 'static') {
          this.failedVideos = [...new Set([...this.failedVideos, item.video_id])].slice(-60);
          const removed = this.selected + 2;
          this.items.splice(removed, 1);
          this.failed = this.failed.filter(index => index !== removed).map(index => index > removed ? index - 1 : index);
          // The rejected entry never had a thumbnail. Its replacement occupies
          // the same hidden slot and is checked before the next advance.
          visualTimer = setTimeout(() => this.inspectAhead(), 0);
          this.refill();
        }
      } catch (_) {
        // An unavailable preview is inconclusive and must not skip a video.
      } finally {
        clearTimeout(timeout);
        if (attempt === visualSerial) { visualRequest = null; visualTarget = null; }
      }
    },
    stopPlayer() {
      if (player) this.report('bye');
      this.cancelVisualCheck();
      ++serial; clearTimeout(timer); clearTimeout(artTimer); controller?.abort(); controller = null;
      ++refillSerial; clearTimeout(refillTimer); refillRequest = null; this.refilling = false; this.waiting = false;
      ++playbackSerial;
      ++selectionSerial;
      ++playerSerial; player?.destroy(); player = null; this.playing = false; this.playerMounted = false; this.connecting = false;
      loadedVideoId = null; releasePending = false; lastPlayerState = null;
      startedSelection = null;
      playbackRetries = 0; retryPending = null;
      this.starting = false; this.loading = false;
      reportedPlay = false;
      ownsPlayback = false; clearInterval(progressTimer); progressTimer = null;
      this.$refs.player.replaceChildren(); this.publish();
    },
    async resume(retrying = false) {
      if (!retrying) playbackRetries = 0;
      const attempt = playerSerial;
      const selection = selectionSerial;
      const intent = playbackSerial;
      try {
        await this.acquire();
        if (intent !== playbackSerial || attempt !== playerSerial || selection !== selectionSerial || this.destroyed) return;
        ownsPlayback = true; releasePending = false; this.playerError = '';
        if (player && loadedVideoId !== this.current?.video_id) {
          loadedVideoId = this.current.video_id; player.loadVideoById(loadedVideoId);
        } else player?.playVideo();
        this.publish();
      } catch (error) {
        if (intent === playbackSerial && attempt === playerSerial && selection === selectionSerial && !this.destroyed) {
          this.connecting = false; this.playerError = error.message;
        }
      }
    },
    async select(index) {
      if (this.destroyed || !this.items[index]) return;
      if (index === this.selected && player && loadedVideoId === this.current.video_id) {
        if (this.playing) player.pauseVideo();
        else await this.resume();
        return;
      }
      if (player && this.current && index !== this.selected) this.report('track_exit');
      this.cancelVisualCheck();
      const selection = ++selectionSerial;
      startedSelection = null;
      playbackRetries = 0; retryPending = null;
      const intent = playbackSerial;
      if (index !== this.selected) reportedPlay = false;
      this.selected = index; this.finished = false; this.playerError = '';
      this.playlistId = this.current.playlist_id || this.playlistId;
      this.playlistTitle = this.current.playlist_title || this.playlistTitle;
      this.waiting = false;
      if (this.current.artist_art_pending) this.refreshArtwork();
      this.revealedCount = Math.min(this.items.length, Math.max(this.revealedCount, index + 2));
      this.revealCurrent();
      if (player?.loadVideoById) {
        try {
          const attempt = playerSerial;
          await this.acquire();
          if (intent !== playbackSerial || attempt !== playerSerial || selection !== selectionSerial || this.destroyed) return;
          ownsPlayback = true; releasePending = false; loadedVideoId = this.current.video_id; player.loadVideoById(loadedVideoId);
        } catch (error) {
          if (intent === playbackSerial && selection === selectionSerial && !this.destroyed) this.playerError = error.message;
        }
        return;
      }
      const attempt = ++playerSerial;
      this.connecting = true;
      try {
        const api = await loadTvApi();
        if (intent !== playbackSerial || attempt !== playerSerial || selection !== selectionSerial || this.destroyed) return;
        const mount = document.createElement('div'); this.$refs.player.replaceChildren(mount);
        this.playerMounted = true;
        loadedVideoId = this.current.video_id;
        player = new api.Player(mount, {width: '100%', height: '100%', videoId: loadedVideoId,
          playerVars: {playsinline: 1, origin: location.origin, autoplay: 0, rel: 0},
          events: {
            onReady: event => {
              if (attempt !== playerSerial || this.destroyed) return;
              const frame = event.target.getIframe?.();
              if (frame) { frame.referrerPolicy = 'strict-origin-when-cross-origin'; frame.allow = 'autoplay; encrypted-media; fullscreen; picture-in-picture'; }
              if (intent === playbackSerial) this.resume();
              this.revealCurrent();
              progressTimer = setInterval(() => {
                if (!ownsPlayback) return;
                this.publish(); this.reportPlay();
                if (this.playing && Date.now() - lastTick >= 15000) { lastTick = Date.now(); this.report('tick'); }
              }, 1000);
            },
            onStateChange: event => {
              if (attempt !== playerSerial || this.destroyed) return;
              // Startup and load transitions can report ENDED before PLAYING.
              // Only consume a completion for the selection that actually ran.
              if (event.data === 0) {
                if (!ownsPlayback || startedSelection !== selectionSerial) return;
                startedSelection = null;
              }
              lastPlayerState = event.data;
              // Native iframe Play also has to acquire the audio player. Pause
              // an old PLAYING notification while our release is awaiting its
              // PAUSED acknowledgement; it is not a new user Play request.
              if (event.data === 1 && !ownsPlayback) {
                const releasing = releasePending;
                this.playing = false; event.target.pauseVideo();
                if (!releasing && this.visible) this.resume();
                return;
              }
              if (event.data === 2) releasePending = false;
              this.playing = event.data === 1;
              if (this.playing) startedSelection = selectionSerial;
              if (this.playing) this.sessionStarted = true;
              if (event.data === 1 || event.data === 2) this.connecting = false;
              if (this.playing) this.reportPlay();
              if (this.playing) { this.refill(); this.inspectAhead(); }
              else this.cancelVisualCheck();
              if (event.data === 1 || event.data === 2) this.report('state', this.playing ? 'playing' : 'paused');
              this.publish();
              if (event.data === 0 && ownsPlayback) { this.report('ended'); this.next(); }
            },
            onAutoplayBlocked: () => {
              if (attempt === playerSerial && !this.destroyed) {
                this.connecting = false;
                this.playerError = 'Press play in the video to start watching.';
              }
            },
            onError: event => {
              if (attempt !== playerSerial || this.destroyed) return;
              this.report('player_error', String(event.data));
              if (event.data === 153) {
                this.playerError = 'YouTube could not verify this player. Reload TuneConsole and try again.';
                this.stopPlayer(); return;
              }
              if (!ownsPlayback || retryPending) return;
              this.playing = false; startedSelection = null;
              this.cancelVisualCheck(); this.publish();
              // A startup error can arrive after a few seconds of PLAYING.
              // Retry the same entry once, including apparent embed failures,
              // before declaring it unavailable. Reuse the normal handoff
              // guards so a delayed retry cannot interrupt another player.
              loadedVideoId = null;
              if (playbackRetries++ === 0) {
                const retry = retryPending = {};
                this.resume(true).finally(() => { if (retryPending === retry) retryPending = null; });
                return;
              }
              if (![2, 100, 101, 150].includes(event.data)) {
                this.connecting = false;
                this.playerError = 'This video could not play. Press play to retry.';
                return;
              }
              this.failed = [...this.failed, this.selected];
              this.failedVideos = [...new Set([...this.failedVideos, this.current.video_id])].slice(-60);
              this.next(true);
            },
          }});
      } catch (_) {
        if (intent === playbackSerial && attempt === playerSerial && !this.destroyed) {
          this.stopPlayer(); this.playerError = 'The video player could not load. Press Play TV to try again.';
        }
      }
    },
    next(unavailable = false) {
      const next = this.items.findIndex((_, index) => index > this.selected && !this.failed.includes(index));
      if (next >= 0) this.select(next);
      else {
        this.playing = false; this.waiting = true; this.connecting = true;
        if (unavailable) this.refillAttempts = Math.max(this.refillAttempts, 10);
        if (this.refillError) this.continuationFailed(this.refillError);
        else this.refill();
        this.publish();
      }
    },
    continuationFailed(message) {
      this.refillError = message;
      if (this.waiting) { this.connecting = false; this.playerError = message; }
    },
    async refill() {
      if (this.refilling || this.refillError || this.destroyed || !ownsPlayback
          || this.items.length - this.selected - 1 > 2) return;
      clearTimeout(refillTimer);
      this.refilling = true;
      const intent = playbackSerial, attempt = ++refillSerial;
      refillRequest ||= window.crypto?.randomUUID?.() || String(Date.now()) + '-' + Math.random().toString(36).slice(2);
      const exclude = [...new Set([...this.failedVideos, ...this.items.slice(this.selected + 1).map(item => item.video_id)])].slice(-60);
      const body = new URLSearchParams({stream: this.stream, request_id: refillRequest,
        current: this.current.video_id, exclude: JSON.stringify(exclude),
        recent: JSON.stringify(this.items.slice(0, this.selected + 1).map(item => item.video_id).slice(-60))});
      try {
        const response = await fetch('/home/tv/playlist/' + this.streamPlaylistId + '/refill', {method: 'POST', body});
        const data = await response.json();
        if (intent !== playbackSerial || attempt !== refillSerial || this.destroyed || !ownsPlayback) return;
        if (response.status === 202 || response.ok && !data.items?.length) {
          if (++this.refillAttempts >= 12) this.continuationFailed('We couldn’t find the next video. Reset TV or choose a longer video length.');
          else refillTimer = setTimeout(() => this.refill(), 2000);
          return;
        }
        if (!response.ok) throw new Error(data.error || 'The next videos could not load. Reset TV to continue.');
        this.stream = data.stream; this.streamPlaylistId = data.playlist_id;
        const upcoming = new Set(this.items.slice(this.selected + 1).map(item => item.video_id));
        const additions = data.items.filter(item => !upcoming.has(item.video_id) && !this.failedVideos.includes(item.video_id));
        this.items.push(...additions.map(item => ({...item, queue_id: ++entry,
          playlist_id: data.playlist_id, playlist_title: data.title})));
        refillRequest = null; this.refillAttempts = 0;
        // Retain at most 24 visited videos and the small prepared lookahead.
        const trim = Math.max(0, this.selected - 23);
        if (trim) {
          this.items.splice(0, trim); this.selected -= trim;
          this.revealedCount = Math.max(0, this.revealedCount - trim);
          this.failed = this.failed.filter(index => index >= trim).map(index => index - trim);
        }
        if (this.waiting && additions.length) this.next();
        else if (!additions.length) refillTimer = setTimeout(() => this.refill(), 2000);
        this.inspectAhead();
      } catch (error) {
        if (intent === playbackSerial && attempt === refillSerial && !this.destroyed) {
          if (++this.refillAttempts >= 3) this.continuationFailed(error.message || 'The next videos could not load. Reset TV to continue.');
          else refillTimer = setTimeout(() => this.refill(), 3000);
        }
      } finally {
        if (attempt === refillSerial) this.refilling = false;
      }
    },
    openParams() {
      const exact = this.lengths.indexOf(this.maxMinutes);
      this.draft = exact >= 0 ? exact : this.lengths.findIndex(value => value === null || value >= this.maxMinutes);
      this.$refs.params.showModal();
    },
    async applyParams() {
      try {
        const response = await fetch('/home/tv', {method: 'POST', body: new URLSearchParams({max_minutes: this.lengths[this.draft] ?? 'unlimited'})});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error);
        this.maxMinutes = data.max_minutes; this.ready = data.ready; this.$refs.params.close();
        this.error = data.message || '';
        if (!this.ready && !this.playerMounted && !this.starting) this.readiness();
      } catch (error) { this.error = error.message || 'Couldn’t change TV parameters.'; }
    },
    externalPlayback() {
      // Revoke pending work without changing the mounted player's lifetime:
      // its callbacks must still work when the user explicitly resumes TV.
      ++playbackSerial;
      this.cancelVisualCheck();
      ++serial; clearTimeout(timer); clearTimeout(artTimer); controller?.abort(); controller = null;
      ++refillSerial; clearTimeout(refillTimer); this.refilling = false; this.waiting = false;
      this.starting = false; this.connecting = false; this.loading = false;
      ownsPlayback = false;
      startedSelection = null;
      retryPending = null;
      releasePending = !!player && (player.getPlayerState?.() ?? lastPlayerState) !== 2;
      player?.pauseVideo(); this.playing = false; this.publish();
    },
    async another() {
      this.stopPlayer(); this.items = []; this.finished = false;
      const intent = playbackSerial;
      try { await fetch('/home/tv/reset', {method: 'POST'}); } catch (_) { /* Start still handles connection errors. */ }
      if (intent === playbackSerial && !this.destroyed) this.start();
    },
    destroy() {
      this.destroyed = true; ++serial; clearTimeout(timer); controller?.abort(); this.stopPlayer();
      viewObserver?.disconnect();
      window.removeEventListener('app-page-changed', updateView);
      window.removeEventListener('hashchange', updateView);
      window.removeEventListener('resize', layoutView);
      window.removeEventListener('animationend', layoutView);
      if (window.tvPlaybackControls === this.controls) delete window.tvPlaybackControls;
    },
  };
}
