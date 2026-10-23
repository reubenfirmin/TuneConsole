// A suggestion-add response stages its table row in a <template>. Moving it here avoids the browser's
// special table parser relocating a bare out-of-band <tr>, while keeping the original grid tile as a
// fixed-size vacancy so the remaining suggestions do not reflow.
document.addEventListener('htmx:afterSwap', (e) => {
  const target = e.detail?.target;
  const tile = target?.closest && target.closest('.tile-suggest');
  const result = tile && tile.querySelector('.suggestion-add-result');
  if (!result) return;
  const template = result.querySelector('.suggestion-track-row');
  const body = document.getElementById('playlist-track-body');
  if (!template || !body) return;
  const row = template.content.firstElementChild;
  if (!row) return;
  body.appendChild(row);
  htmx.process(row);
  const count = document.getElementById('playlist-track-count');
  if (count) count.textContent = result.dataset.trackCount;
  tile.classList.remove('suggestion-departing');
  tile.classList.add('suggestion-vacancy');
  tile.setAttribute('aria-hidden', 'true');
  document.dispatchEvent(new CustomEvent('playlist-track-added'));
});

// On a track-list page (playlist, album): highlight the currently-playing row (matched by video id,
// falling back to a normalized title). Metadata enrichment has its own loop, independent of
// playback status, so it continues while the player is disconnected or another list is playing.
(function () {
  if (window.__trackPlaybackBound) return;
  window.__trackPlaybackBound = true;
  function norm(s) {
    return (s || '').toLowerCase().replace(/\(.*?\)|\[.*?\]/g, '').replace(/\bfeat\.?.*$/, '')
      .replace(/[^a-z0-9]+/g, ' ').trim();
  }
  function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
  function rowByVid(vid) {
    var sel = window.CSS && CSS.escape ? CSS.escape(vid) : vid;
    return document.querySelector('tr[data-vid="' + sel + '"]');
  }
  var metaDone = false, metadataTable = null;
  document.addEventListener('playlist-track-added', function () {
    metaDone = false;
    refreshCells();
    highlightCurrent();
  });
  window.addEventListener('song-metadata-updated', function () {
    document.body.dispatchEvent(new CustomEvent('playlist-metadata-changed'));
  });

  // Returns true if the now-playing track is one of the rows on this page.
  function highlight(np) {
    var rows = document.querySelectorAll('tr[data-vid]');
    rows.forEach(function (r) { r.classList.remove('now-playing'); });
    if (!np || !np.title) return false;
    var match = np.video_id ? rowByVid(np.video_id) : null;
    if (!match) {
      var nt = norm(np.title);
      rows.forEach(function (r) { if (!match && norm(r.getAttribute('data-title')) === nt) match = r; });
    }
    if (match) { match.classList.add('now-playing'); return true; }
    return false;
  }

  function refreshCells() {
    var plId = (location.pathname.match(/^\/playlist\/(\d+)/) || [])[1];
    var table = document.querySelector('tr[data-vid]')?.closest('table');
    if (table !== metadataTable) { metadataTable = table; metaDone = false; }
    if (!plId || !table || metaDone) return;
    fetch('/playlist/' + plId + '/cells').then(function (r) { return r.json(); }).then(function (d) {
      if (!table.isConnected || (location.pathname.match(/^\/playlist\/(\d+)/) || [])[1] !== plId) return;
      (d.cells || []).forEach(function (c) {
        var row = rowByVid(c.video_id); if (!row) return;
        if (c.genre) {
          var g = row.querySelector('.genre-display-wrap');
          if (g && !g.querySelector('.gtag')) {
            var hint = g.querySelector('.ghint');
            if (hint) hint.outerHTML = '<span class="gtag">' + esc(c.genre) + '</span>';
            row.setAttribute('data-genre', String(c.genre).toLowerCase());
          }
        }
        if (c.year) {
          var y = row.querySelector('.ydisplay');
          if (y && y.querySelector('.ghint')) { y.textContent = c.year; row.setAttribute('data-year', String(c.year)); }
        }
      });
      var facets = document.getElementById('fb-facets-' + plId);
      if (facets && d.feedback_version && facets.dataset.metadataVersion !== d.feedback_version) {
        document.body.dispatchEvent(new CustomEvent('playlist-metadata-changed'));
      }
      if (d.pending === 0) metaDone = true;   // everything resolved: stop refreshing cells
    }).catch(function () {});
  }

  function highlightCurrent() {
    var data = window.playbackStatus.snapshot;
    highlight(data?.connected ? data.now_playing : null);
  }
  window.addEventListener('bridge-status', function (event) {
    highlight(event.detail?.connected ? event.detail.now_playing : null);
  });
  document.addEventListener('htmx:afterSwap', highlightCurrent);
  document.addEventListener('DOMContentLoaded', function () { highlightCurrent(); refreshCells(); }, {once: true});
  highlightCurrent();
  refreshCells();
  setInterval(refreshCells, 2500);
})();

