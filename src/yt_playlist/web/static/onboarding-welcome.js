function onboardingWelcome() {
  return {
    connected: !!window.playbackStatus?.snapshot?.connected,
    init() {
      this._dialog = this.$el;
      this._bridge = event => { this.connected = !!event.detail.connected; };
      window.addEventListener('bridge-status', this._bridge);
      this.$nextTick(() => {
        if (window.__onboardingWelcomeDismissed) this.dismiss();
        else this._dialog.showModal();
      });
    },
    dismiss() {
      window.__onboardingWelcomeDismissed = true;
      this._dialog.close();
      fetch('/onboard/welcome/dismiss', {method: 'POST', signal: AbortSignal.timeout(3000)})
        .catch(error => console.debug('Welcome acknowledgement deferred', error));
    },
    destroy() { window.removeEventListener('bridge-status', this._bridge); },
  };
}
