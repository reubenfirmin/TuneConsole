(() => {
  function start() {
    let pending = false;
    async function refresh() {
      if (pending) return;
      pending = true;
      try {
        const response = await fetch('/storage-maintenance/status', {cache: 'no-store'});
        if (!response.ok) return;
        const state = await response.json();
        document.documentElement.dataset.storageCompacting = state.active ? 'true' : 'false';
        const banner = document.getElementById('storage-maintenance');
        if (banner) {
          const message = state.warning || state.phase === 'recovery_required' ? '' : state.banner_message || '';
          if (banner.getAttribute('role') !== 'status') banner.setAttribute('role', 'status');
          if (banner.getAttribute('aria-live') !== 'polite') banner.setAttribute('aria-live', 'polite');
          banner.dataset.warning = 'false';
          if (banner.textContent !== message) banner.textContent = message;
          banner.hidden = !message;
        }
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
