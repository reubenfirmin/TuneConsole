// One setup poll per document. Home and the persistent banner share this evidence.
(function () {
  let timer, controller, stopped = false;
  window.setupStatus = {snapshot: null};
  async function refresh() {
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/home/onboard/status', {signal: controller.signal});
      if (!response.ok) throw new Error('Setup status unavailable');
      const data = await response.json();
      if (stopped) return;
      window.setupStatus.snapshot = data;
      window.dispatchEvent(new CustomEvent('setup-status', {detail: data}));
      if (data.setup.complete) stopped = true;
    } catch (_) {
      // Keep the last real state during a reconnect; never invent progress.
    } finally {
      clearTimeout(timeout);
      if (!stopped) timer = setTimeout(refresh, document.hidden ? 15000 : 3000);
    }
  }
  document.addEventListener('DOMContentLoaded', refresh, {once: true});
  window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); controller?.abort(); });
  window.addEventListener('pageshow', event => {
    if (event.persisted && !window.setupStatus.snapshot?.setup.complete) { stopped = false; refresh(); }
  });
})();

function initialSetup() {
  return {
    complete: false, connected: false, running: false, training: false, imported: false,
    songs: 0, analyzed: 0, total: 0, modes: 0, mixes_ready: false,
    activity: '', current: null, target: null, step: '', outcome: null, events: [], _logRequest: false,
    get summary() {
      if (!this.imported) {
        if (this.running && this.activity && this.current && this.target) {
          return 'Reading ' + (this.activity === 'albums' ? 'album ' : 'playlist ') + this.current + ' of ' + this.target;
        }
        if (['partial', 'failed', 'skipped'].includes(this.outcome)) return 'Some music will need another try';
        if (this.songs || this.running) return 'Bringing in your music';
        return this.connected ? 'Keep YouTube Music open' : 'Connect the extension to get started';
      }
      return 'Preparing your personal mixes';
    },
    apply(data) {
      Object.assign(this, data.setup, {songs: data.songs, outcome: data.outcome});
      this.$nextTick(() => this.measure());
      if (this.$refs.details.open) this.loadDetails();
    },
    measure() {
      document.documentElement.style.setProperty('--setup-banner-height', this.complete ? '0px' : this.$el.offsetHeight + 'px');
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
      this.connected = !!window.playbackStatus?.snapshot?.connected;
      if (window.setupStatus?.snapshot) this.apply(window.setupStatus.snapshot);
      this._observer = new ResizeObserver(() => this.measure());
      this._observer.observe(this.$el);
    },
    destroy() { this._observer.disconnect(); document.documentElement.style.removeProperty('--setup-banner-height'); },
  };
}
