// Exposure belongs to visible discovery tiles and story headings, never a GET or hidden tab.
// A page's seen set survives fragment polls. Observe the heading rather than a whole article,
// which can be taller than the viewport on a phone.
let discoveryExposure = null;
function syncDiscoveryExposure() {
  const home = document.getElementById('home');
  if (discoveryExposure?.home !== home) {
    discoveryExposure?.observer.disconnect();
    discoveryExposure?.timers.forEach(clearTimeout);
    discoveryExposure = null;
  }
  if (!home || !window.IntersectionObserver) return;
  if (!discoveryExposure) {
    const state = {home, viewId: crypto.randomUUID(), observed: new Set(), seen: new Set(),
      visible: new Set(), timers: new Map()};
    state.observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        const tile = entry.target;
        if (!entry.isIntersecting || entry.intersectionRatio < .5) {
          state.visible.delete(tile);
          clearTimeout(state.timers.get(tile));
          state.timers.delete(tile);
          return;
        }
        state.visible.add(tile);
        if (document.hidden || state.timers.has(tile)) return;
        const story = tile.dataset.storySubject;
        const kind = story ? 'story' : tile.dataset.discoveryKind;
        const id = story || tile.dataset.discoveryId;
        const key = JSON.stringify([kind, id]);
        if (state.seen.has(key)) return;
        state.timers.set(tile, setTimeout(() => {
          state.timers.delete(tile);
          if (document.hidden || !tile.isConnected || !tile.getClientRects().length ||
              !state.visible.has(tile) || state.seen.has(key)) return;
          state.seen.add(key);
          fetch(story ? '/home/into-recently/impressions' : '/discovery/impressions', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, keepalive: true,
            body: JSON.stringify(story ? {subjects: [id]} : {kind, ids: [id], view_id: state.viewId})
          }).then(response => {
            if (!response.ok) state.seen.delete(key);
          }).catch(() => state.seen.delete(key));
        }, 750));
      });
    }, {threshold: .5});
    discoveryExposure = state;
  }
  const state = discoveryExposure;
  state.observed.forEach(tile => {
    if (tile.isConnected) return;
    state.observer.unobserve(tile);
    state.observed.delete(tile);
    state.visible.delete(tile);
    clearTimeout(state.timers.get(tile));
    state.timers.delete(tile);
  });
  home.querySelectorAll('[data-discovery-id], [data-story-subject]').forEach(tile => {
    if (state.observed.has(tile)) return;
    state.observed.add(tile);
    state.observer.observe(tile);
  });
}
document.addEventListener('DOMContentLoaded', syncDiscoveryExposure);
document.addEventListener('htmx:afterSettle', syncDiscoveryExposure);
document.addEventListener('htmx:beforeCleanupElement', event => {
  if (event.detail.elt !== discoveryExposure?.home) return;
  discoveryExposure.observer.disconnect();
  discoveryExposure.timers.forEach(clearTimeout);
  discoveryExposure = null;
});
document.addEventListener('visibilitychange', () => {
  if (!discoveryExposure) return;
  discoveryExposure.timers.forEach(clearTimeout);
  discoveryExposure.timers.clear();
  if (!document.hidden) discoveryExposure.observed.forEach(tile => {
    discoveryExposure.observer.unobserve(tile);
    discoveryExposure.observer.observe(tile);
  });
});

