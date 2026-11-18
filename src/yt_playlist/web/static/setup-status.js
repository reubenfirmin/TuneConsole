// One setup poll per document. Home and the persistent banner share this evidence.
(function () {
  if (window.setupStatus) return;
  let timer, controller, progressController, stopped = true, detailedAt = -Infinity;
  window.setupStatus = {snapshot: null, start() {
    if (!stopped) return;
    stopped = false;
    refresh();
  }};
  async function detailed(final = false) {
    if (controller || !final && Date.now() - detailedAt < 5000) return false;
    detailedAt = Date.now();
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/home/onboard/status', {signal: controller.signal});
      if (!response.ok) return false;
      const data = await response.json();
      if (stopped) return false;
      window.setupStatus.snapshot = data;
      window.dispatchEvent(new CustomEvent('setup-status', {detail: data}));
      return !!data.setup.complete;
    } catch (_) { return false; } finally { clearTimeout(timeout); controller = null; }
  }
  async function refresh() {
    if (progressController) return;
    progressController = new AbortController();
    const timeout = setTimeout(() => progressController.abort(), 5000);
    try {
      const response = await fetch('/home/onboard/progress', {signal: progressController.signal});
      if (!response.ok) throw new Error('Setup status unavailable');
      const data = await response.json();
      if (stopped) return;
      window.dispatchEvent(new CustomEvent('setup-progress', {detail: data}));
      if (data.complete && !data.running) stopped = await detailed(true);
      else detailed();
    } catch (_) {
      // Keep the last real state during a reconnect; never invent progress.
    } finally {
      clearTimeout(timeout);
      progressController = null;
      if (!stopped) timer = setTimeout(refresh, document.hidden ? 15000 : 3000);
    }
  }
  const mount = () => {
    if (document.querySelector('.setup-status')?.dataset.setupPending === 'true') window.setupStatus.start();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, {once: true});
  else mount();
  window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); controller?.abort(); progressController?.abort(); });
  window.addEventListener('pageshow', event => {
    if (event.persisted) mount();
  });
})();

function initialSetup() {
  return {
    complete: false, connected: false, running: false, training: false, imported: false,
    songs: 0, analyzed: 0, total: 0, modes: 0, mixes_ready: false, listening_format: 'mixes', artists_required: false,
    activity: '', current: null, target: null, completed: 0, step: '', outcome: null, events: [], _logRequest: false,
    get summary() {
      if (this.artists_required) return 'First sounds';
      if (!this.imported || this.running) {
        if (this.running && this.activity && this.current && this.target) {
          const item = {albums: 'album', playlists: 'playlist', songs: 'song'}[this.activity];
          if (item) return 'Reading ' + item + ' ' + this.current + ' of ' + this.target;
        }
        if (this.songs || this.running) return 'Bringing in your music';
        return this.connected ? 'Keep YouTube Music open' : 'Connect the extension to get started';
      }
      if (this.listening_format === 'albums') return 'Finding albums for you';
      if (this.listening_format === 'radio') return 'Getting your radio ready';
      return 'Preparing your personal mixes';
    },
    apply(data) {
      Object.assign(this, data.setup, {songs: data.songs, outcome: data.outcome});
      this.complete = data.setup.complete;
      this.$nextTick(() => this.measure());
      if (this.$refs.details.open) this.loadDetails();
    },
    applyProgress(data) {
      Object.assign(this, data);
      this.$nextTick(() => this.measure());
    },
    measure() {
      document.documentElement.style.setProperty('--setup-banner-height', this.complete && !this.running ? '0px' : this.$el.offsetHeight + 'px');
    },
    async loadDetails() {
      if (this._logRequest) return;
      this._logRequest = true;
      try {
        const response = await fetch('/sync/recent');
        if (response.ok) this.events = (await response.json()).events || [];
      } catch (_) {} finally { this._logRequest = false; }
    },
    openDetails() { this.$refs.details.showModal(); this.loadDetails(); },
    init() {
      this.complete = this.$el.dataset.setupPending !== 'true';
      this.connected = !!window.playbackStatus?.snapshot?.connected;
      if (window.setupStatus?.snapshot) this.apply(window.setupStatus.snapshot);
      this._observer = new ResizeObserver(() => this.measure());
      this._observer.observe(this.$el);
    },
    destroy() { this._observer.disconnect(); document.documentElement.style.removeProperty('--setup-banner-height'); },
  };
}
