// Platform updaters own installation; the card starts updates and reconnects after relaunch.
function appUpdate() {
  return {
    phase: 'available', message: '', progress: null, instance: null,
    pending: false, timer: null, destroyed: false, pollId: 0, canCancel: false, restartDeadline: null,
    get working() { return this.pending || ['installing', 'draining', 'launching', 'restarting'].includes(this.phase); },
    init() { this.refresh(); },
    destroy() { this.destroyed = true; clearTimeout(this.timer); },
    apply(state) {
      this.phase = state.phase;
      this.message = state.message;
      this.progress = state.progress;
      this.instance = state.instance;
      this.canCancel = !!state.can_cancel;
      if (state.phase === 'restarting') {
        this.restartDeadline ??= Date.now() + 60000;
        if (Date.now() >= this.restartDeadline) {
          this.phase = 'reconnect';
          this.message = 'Couldn’t reconnect. Open TuneConsole from your applications menu, then reload this tab.';
        }
      } else this.restartDeadline = null;
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
        if (pollId === this.pollId && !this.destroyed && !this.pending) {
          if (this.restartDeadline && Date.now() >= this.restartDeadline) {
            this.phase = 'reconnect';
            this.message = 'Couldn’t reconnect. Open TuneConsole from your applications menu, then reload this tab.';
          } else if (this.phase !== 'restarting') this.message = 'Couldn’t check the update. Please try again.';
        }
      }
      if (!this.destroyed && !this.pending && pollId === this.pollId)
        this.timer = setTimeout(() => this.refresh(), this.phase === 'restarting' ? 1000 : 2000);
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
      try {
        const state = await this.request('restart', 'POST');
        if (!this.destroyed) this.apply(state);
      } catch (error) {
        if (!this.destroyed) this.message = error.message;
      } finally {
        this.pending = false;
        this.refresh();
      }
    },
    async cancelRestart() {
      if (!this.canCancel || this.pending) return;
      this.pending = true;
      this.pollId++;
      clearTimeout(this.timer);
      try { this.apply(await this.request('restart/cancel', 'POST')); }
      catch (error) { this.message = error.message; }
      finally { this.pending = false; this.refresh(); }
    },
    reconnect() { window.location.reload(); }
  };
}
