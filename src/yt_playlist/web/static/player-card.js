function mountNowPlayingDock() {
  const card = document.querySelector('.global-now-playing');
  const slot = document.querySelector('.sidebar-dock-slot');
  const origin = document.querySelector('.global-now-playing-slot');
  if (!card || !slot || !origin) return;
  const standalone = document.body.classList.contains('app-standalone');
  const target = !standalone && window.matchMedia('(min-width: 65rem)').matches ? slot : origin;
  if (card.parentElement !== target) target.appendChild(card);
  const measure = () => {
    const height = document.body.classList.contains('app-standalone') && card.getClientRects().length
      ? origin.getBoundingClientRect().height : 0;
    document.body.style.setProperty('--playback-dock-height', height + 'px');
  };
  measure();
  if (!window.__homeDockBound) {
    window.__homeDockBound = true;
    window.addEventListener('resize', mountNowPlayingDock);
    window.addEventListener('app-page-changed', mountNowPlayingDock);
    new ResizeObserver(measure).observe(card);
  }
}

function playerStatus() {
  return {
    connected: false,
    nowPlaying: null,
    timing: {position: null, duration: null},
    metadataOverrides: {},
    pendingRating: null,
    ratingSequence: 0,
    ratingTail: null,
    confirmedRatings: {},
    skipping: false,
    artworkFailed: '',
    currentLocation: location.href,
    sourceLinkVisible() {
      const source = this.nowPlaying && this.nowPlaying.source;
      if (!source || source.external || !source.url) return false;
      const target = new URL(source.url, location.href);
      const current = new URL(this.currentLocation);
      if (target.origin !== current.origin) return false;
      if (target.pathname.replace(/\/+$/, '') !== current.pathname.replace(/\/+$/, '')) return true;
      if (source.kind === 'radio') return current.hash !== '#radio';
      if (source.kind === 'tv') return current.hash !== '#tv';
      return source.kind === 'album' &&
        target.searchParams.get('browse') !== current.searchParams.get('browse');
    },
    sourceName() {
      const kind = this.nowPlaying?.source?.kind || '';
      return kind === 'radio' ? 'Radio' : kind === 'tv' ? 'TV' : kind;
    },
    init() {
      window.playbackStatus.start();
      if (window.playbackStatus.snapshot) this.apply(window.playbackStatus.snapshot);
    },
    publishLike() {
      if (!this.nowPlaying || !this.nowPlaying.video_id) return;
      window.dispatchEvent(new CustomEvent('track-like-changed', { detail: {
        videoId: this.nowPlaying.video_id,
        liked: this.nowPlaying.likeStatus === 'LIKE',
        likeStatus: this.nowPlaying.likeStatus,
      }}));
    },
    updateMetadata(detail) {
      this.metadataOverrides[detail.videoId] = { title: detail.title, artist: detail.artist };
      if (this.nowPlaying?.video_id === detail.videoId) Object.assign(this.nowPlaying, this.metadataOverrides[detail.videoId]);
    },
    applyProgress(progress) {
      this.timing = progress && progress.videoId === this.nowPlaying?.video_id ? progress :
        {position: null, duration: null};
    },
    timeLabel(seconds) {
      if (seconds === null || !Number.isFinite(seconds)) return '--:--';
      const total = Math.floor(seconds), minutes = Math.floor(total / 60);
      const tail = String(total % 60).padStart(2, '0');
      return minutes >= 60 ? Math.floor(minutes / 60) + ':' + String(minutes % 60).padStart(2, '0') + ':' + tail :
        minutes + ':' + tail;
    },
    apply(d) {
      this.connected = !!d.connected;
      const next = d.now_playing ? { ...d.now_playing } : null;
      if (next) Object.assign(next, this.metadataOverrides[next.video_id]);
      if (next && !this.pendingRating) this.confirmedRatings[next.video_id] = next.likeStatus;
      const pending = this.pendingRating;
      if (pending) {
        if (!this.connected || !next || next.video_id !== pending.videoId ||
            (pending.settled && (next.likeStatus === pending.likeStatus || Date.now() >= pending.expires))) {
          this.pendingRating = null;
        } else {
          // A status request can finish before the rating API write. Keep both hearts on the
          // requested state until the backend confirms it, with a bounded wait.
          next.likeStatus = pending.likeStatus;
        }
      }
      this.nowPlaying = next;
      this.applyProgress(window.playbackStatus.progress());
      const icon = document.querySelector('link[rel="icon"][data-playing-href]');
      if (icon) {
        icon.dataset.idleHref ||= icon.getAttribute('href');
        const playing = this.connected && next?.paused === false && !!next.video_id;
        const href = playing ? icon.dataset.playingHref : icon.dataset.idleHref;
        if (icon.getAttribute('href') !== href) icon.setAttribute('href', href);
      }
      this.publishLike();
    },
    requestLike(event) {
      const { videoId, liked } = event.detail;
      if (!this.connected || !this.nowPlaying || this.nowPlaying.video_id !== videoId) return;
      // Share the backend rating action with the sidebar so both hearts update together.
      event.preventDefault();
      this.rate('like', liked ? 'LIKE' : 'INDIFFERENT');
    },
    rate(action, desiredStatus = null) {
      if (!this.nowPlaying || this.skipping) return;
      const want = action === 'like' ? 'LIKE' : 'DISLIKE';
      const previous = this.nowPlaying.likeStatus;
      const videoId = this.nowPlaying.video_id;
      if (!(videoId in this.confirmedRatings)) this.confirmedRatings[videoId] = previous;
      const sequence = ++this.ratingSequence;
      this.nowPlaying.likeStatus = desiredStatus || (previous === want ? 'INDIFFERENT' : want);
      const likeStatus = this.nowPlaying.likeStatus;
      const advancing = likeStatus === 'DISLIKE';
      if (advancing) this.skipping = true;
      this.pendingRating = { sequence, videoId, likeStatus: this.nowPlaying.likeStatus,
                             settled: false, expires: Date.now() + 10000 };
      this.publishLike();
      const revert = () => {
        if (this.pendingRating?.sequence !== sequence) return;
        this.pendingRating = null;
        if (this.nowPlaying?.video_id === videoId) {
          this.nowPlaying.likeStatus = this.confirmedRatings[videoId];
          this.publishLike();
        }
      };
      // Preserve click order before HTTP dispatch too. All backend operations instances share
      // a per-account/upload queue, including API retries and native thumb reconciliation.
      const send = () => fetch('/now-playing/rate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, videoId, likeStatus }) });
      const task = (this.ratingTail || Promise.resolve()).then(send)
        .then(r => r.json()).then(d => {
          if (!d.ok) {
            console.debug('[Rating] update rejected', videoId, d.error);
            revert();
          } else {
            this.confirmedRatings[videoId] = likeStatus;
            if (window.tvPlaybackControls?.active()) window.tvPlaybackControls.rated(videoId, likeStatus);
            if (d.player_error) console.debug('[Rating] native player update not confirmed', videoId, d.player_error);
          }
        }).catch(error => {
          console.debug('[Rating] request interrupted', videoId, error);
          // A lost response does not prove the remote write was cancelled. Refresh retained
          // evidence instead of rolling back to an earlier optimistic choice.
          if (this.pendingRating?.sequence === sequence) this.pendingRating = null;
        }).finally(() => {
          if (this.pendingRating?.sequence === sequence) {
            this.pendingRating.settled = true;
            this.pendingRating.expires = Date.now() + 10000;
          }
          if (advancing) this.skipping = false;
          window.playbackStatus.refresh();
          if (this.ratingTail === task) this.ratingTail = null;
        });
      this.ratingTail = task;
      return task;
    },
    async next() {
      if (!this.nowPlaying?.video_id || this.skipping) return;
      if (window.tvPlaybackControls?.active()) return window.tvPlaybackControls.command('next');
      const videoId = this.nowPlaying.video_id;
      this.skipping = true;
      try {
        const response = await fetch('/now-playing/next', { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ videoId }) });
        const result = await response.json();
        if (!result.ok) throw new Error(result.error);
      } catch (error) {
        console.debug('[Playback] skip not yet confirmed', videoId, error);
      } finally {
        this.skipping = false;
      }
    },
    toggle() {
      if (!this.nowPlaying) return;
      if (window.tvPlaybackControls?.active()) return window.tvPlaybackControls.command('toggle');
      this.nowPlaying.paused = !this.nowPlaying.paused;
      fetch('/now-playing/toggle', { method: 'POST' }).catch(() => {});
    },
  };
}
window.mountNowPlayingDock = mountNowPlayingDock;
