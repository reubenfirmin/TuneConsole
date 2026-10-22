// Platform updaters own installation; the card starts updates and reconnects after relaunch.
function appUpdate() {
  return {
    phase: 'available', message: '', progress: null, instance: null,
    pending: false, timer: null, destroyed: false, pollId: 0,
    get working() { return this.pending || this.phase === 'installing' || this.phase === 'restarting'; },
    init() { this.refresh(); },
    destroy() { this.destroyed = true; clearTimeout(this.timer); },
    apply(state) {
      this.phase = state.phase;
      this.message = state.message;
      this.progress = state.progress;
      this.instance = state.instance;
    },
    async request(path, method = 'GET') {
      const response = await fetch('/app-update/' + path, { method, cache: 'no-store' });
      const state = await response.json();
      if (!response.ok && !state.busy) throw new Error(state.message || 'Please try again.');
      return state;
    },
    async refresh() {
      if (this.destroyed || this.pending) return;
      const pollId = ++this.pollId;
      try {
        const state = await this.request('status');
        if (pollId === this.pollId && !this.destroyed && !this.pending) {
          // Sparkle's Install and Relaunch happens in a native window. The old listener
          // may close between polls, so detect the new process even without a final status.
          if (this.instance && state.instance !== this.instance) {
            window.location.reload();
            return;
          }
          this.apply(state);
        }
      } catch (_) {
        if (pollId === this.pollId && !this.pending) this.message = 'Couldn’t check the update. Please try again.';
      }
      if (!this.destroyed && !this.pending && pollId === this.pollId)
        this.timer = setTimeout(() => this.refresh(), 2000);
    },
    async install() {
      if (this.working) return;
      this.pending = true;
      this.pollId++;
      clearTimeout(this.timer);
      this.message = 'Requesting the update…';
      try { this.apply(await this.request('install', 'POST')); }
      catch (error) { this.phase = 'error'; this.message = error.message; }
      finally { this.pending = false; this.refresh(); }
    },
    async restart() {
      if (this.working) return;
      this.pending = true;
      this.pollId++;
      clearTimeout(this.timer);
      const oldInstance = this.instance;
      try {
        let state;
        do {
          state = await this.request('restart', 'POST');
          this.message = state.message;
          if (state.busy) await new Promise(resolve => setTimeout(resolve, 2000));
        } while (state.busy && !this.destroyed);
        if (this.destroyed) return;
        if (state.phase !== 'restarting') {
          this.apply(state);
          return;
        }
        this.phase = 'restarting';
        this.message = 'Restarting TuneConsole…';
        const deadline = Date.now() + 60000;
        while (Date.now() < deadline && !this.destroyed) {
          await new Promise(resolve => setTimeout(resolve, 1000));
          try {
            const current = await this.request('status');
            if (current.instance && current.instance !== oldInstance) {
              window.location.reload();
              return;
            }
          } catch (_) { /* The listener is expected to disappear during the handoff. */ }
        }
        this.phase = 'reconnect';
        this.message = 'Couldn’t reconnect. Open TuneConsole from your applications menu, then reload this tab.';
      } catch (error) {
        this.phase = 'ready';
        this.message = error.message;
      } finally {
        this.pending = false;
        if (!['restarting', 'reconnect'].includes(this.phase)) this.refresh();
      }
    },
    reconnect() { window.location.reload(); }
  };
}
