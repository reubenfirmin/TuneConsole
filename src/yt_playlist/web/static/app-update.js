// Platform updaters own installation; the card starts updates and reconnects after relaunch.
function appUpdate() {
  return {
    phase: 'available', message: '', progress: null, instance: null,
    pending: false, uncertain: false, timer: null, destroyed: false, pollId: 0, canCancel: false, restartDeadline: null,
    get working() { return this.pending || this.uncertain || ['installing', 'draining', 'launching', 'restarting'].includes(this.phase); },
    init() { this.refresh(); },
    destroy() { this.destroyed = true; clearTimeout(this.timer); },
    apply(state) {
      this.uncertain = false;
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
      const response = await fetch('/app-update/' + path, { method, cache: 'no-store', signal: AbortSignal.timeout(20000) });
      if (!response.ok && response.status !== 409) {
        console.debug('[App update] response unavailable', response.status, await response.text());
        throw new Error('Update request failed');
      }
      const state = await response.json();
      if ((!response.ok && !state.busy) || !['available', 'installing', 'ready', 'draining', 'launching',
          'restarting', 'current', 'error', 'unavailable', 'manual_restart'].includes(state.phase)
          || typeof state.instance !== 'string' || !state.instance || typeof state.message !== 'string') {
        console.debug('[App update] invalid response', response.status, state);
        throw new Error('Update response unavailable');
      }
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
            if (window.dispatchEvent(new Event('app-before-auto-reload', {cancelable: true}))) {
              window.location.reload();
              return;
            }
            // Retry after the listener finishes using an open feedback panel.
          } else {
            this.apply(state);
          }
        }
      } catch (error) {
        console.debug('[App update] status unavailable', error);
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
      try {
        const state = await this.request('install', 'POST');
        if (!this.destroyed) this.apply(state);
      } catch (error) {
        console.debug('[App update] install request not confirmed', error);
        if (!this.destroyed) {
          this.uncertain = true;
          this.message = 'Update request not confirmed. Checking its progress…';
        }
      }
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
        console.debug('[App update] restart request not confirmed', error);
        if (!this.destroyed) {
          this.uncertain = true;
          this.message = 'Restart request not confirmed. Checking its progress…';
        }
      } finally {
        this.pending = false;
        this.refresh();
      }
    },
    async cancelRestart() {
      if (!this.canCancel || this.pending || this.uncertain) return;
      this.pending = true;
      this.pollId++;
      clearTimeout(this.timer);
      try {
        const state = await this.request('restart/cancel', 'POST');
        if (!this.destroyed) this.apply(state);
      } catch (error) {
        console.debug('[App update] cancellation not confirmed', error);
        if (!this.destroyed) {
          this.uncertain = true;
          this.message = 'Cancellation not confirmed. Checking the restart…';
        }
      }
      finally { this.pending = false; this.refresh(); }
    },
    reconnect() { window.location.reload(); }
  };
}
