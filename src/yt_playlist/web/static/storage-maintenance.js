(() => {
  function start() {
    const banner = document.getElementById('storage-maintenance');
    if (!banner) return;
    let pending = false;
    async function refresh() {
      if (pending) return;
      pending = true;
      try {
        const response = await fetch('/storage-maintenance/status', {cache: 'no-store'});
        if (!response.ok) return;
        const state = await response.json();
        const warning = !!state.warning;
        const visible = state.active || warning;
        if (banner.dataset.warning !== String(warning)) {
          banner.dataset.warning = String(warning);
          banner.setAttribute('role', warning ? 'alert' : 'status');
          banner.setAttribute('aria-live', warning ? 'assertive' : 'polite');
        }
        const message = visible ? state.message || '' : '';
        if (banner.textContent !== message) banner.textContent = message;
        banner.hidden = !visible;
        document.documentElement.dataset.storageCompacting = state.active ? 'true' : 'false';
        if (Array.isArray(state.auth_labels)) {
          window.dispatchEvent(new CustomEvent('auth-checked', {detail: {labels: state.auth_labels}}));
        }
      } catch (_) {
        // Preserve the last known recording state during a temporary disconnect.
      } finally {
        pending = false;
      }
    }
    refresh();
    window.setInterval(refresh, 2000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
