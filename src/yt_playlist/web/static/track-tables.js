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
    return (s || '').normalize('NFKC').toLowerCase()
      .replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
  }
  function rowByVid(vid) {
    var sel = window.CSS && CSS.escape ? CSS.escape(vid) : vid;
    return document.querySelector('tr[data-vid="' + sel + '"]');
  }
  var metaDone = false, metadataTable = null, metadataRequest = null, playsRequest = null;
  function cancelReads() {
    metadataRequest?.abort(); metadataRequest = null;
    playsRequest?.abort(); playsRequest = null;
  }
  window.addEventListener('app-page-leaving', cancelReads);
  window.addEventListener('pagehide', cancelReads);
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
    if (!np) return false;
    var matches = np.video_id ? Array.from(rows).filter(r => r.getAttribute('data-vid') === np.video_id) : [];
    // YouTube can play another upload of the requested song. Prefer its exact
    // video ID, then require both title and artist rather than losing the row.
    // Keep version qualifiers so a live recording cannot select a studio row.
    if (!matches.length && np.title && np.artist) {
      var nt = norm(np.title);
      rows.forEach(function (r) {
        if (nt && norm(r.getAttribute('data-title')) === nt &&
            norm(r.getAttribute('data-artist')) === norm(np.artist)) matches.push(r);
      });
    }
    matches.forEach(r => r.classList.add('now-playing'));
    return matches.length > 0;
  }

  function refreshCells() {
    var plId = (location.pathname.match(/^\/playlist\/(\d+)/) || [])[1];
    var table = document.querySelector('tr[data-vid]')?.closest('table');
    if (table !== metadataTable) {
      metadataRequest?.abort(); metadataRequest = null;
      metadataTable = table; metaDone = false;
    }
    if (!plId || !table || metaDone || metadataRequest) return;
    var request = metadataRequest = new AbortController();
    fetch('/playlist/' + plId + '/cells', {signal: request.signal}).then(function (r) { return r.json(); }).then(function (d) {
      if (!table.isConnected || (location.pathname.match(/^\/playlist\/(\d+)/) || [])[1] !== plId) return;
      (d.cells || []).forEach(function (c) {
        var row = rowByVid(c.video_id); if (!row) return;
        if (c.genre) {
          var g = row.querySelector('.genre-display-wrap');
          if (g && !g.querySelector('.gtag')) {
            var hint = g.querySelector('.ghint');
            if (hint) {
              var badge = document.createElement('span');
              badge.className = 'gtag';
              badge.title = c.genre;
              var label = document.createElement('span');
              label.className = 'gtag-label';
              label.textContent = c.genre;
              badge.append(label);
              hint.replaceWith(badge);
            }
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
    }).catch(function () {}).finally(function () {
      if (metadataRequest === request) metadataRequest = null;
    });
  }

  function highlightCurrent() {
    var data = window.playbackStatus.snapshot;
    highlight(data?.connected ? data.now_playing : null);
  }
  async function refreshPlays() {
    if (playsRequest || document.hidden) return;
    var rows = [...document.querySelectorAll('tr[data-vid]:has(.track-plays)')];
    var videos = [...new Set(rows.map(row => row.dataset.vid).filter(Boolean))];
    if (!videos.length) return;
    var request = playsRequest = new AbortController();
    try {
      for (var offset = 0; offset < videos.length; offset += 500) {
        request.signal.throwIfAborted();
        var response = await fetch('/album/track-plays?videos=' + encodeURIComponent(videos.slice(offset, offset + 500).join(',')),
          {signal: request.signal});
        if (!response.ok) continue;
        var counts = (await response.json()).plays || {};
        rows.forEach(function (row) {
          var count = counts[row.dataset.vid];
          if (!row.isConnected || !Number.isInteger(count) || count < 0) return;
          row.dataset.plays = count;
          row.querySelector('.track-plays .track-cell').textContent = count || '';
        });
      }
    } catch (_) { /* Keep the last confirmed counts during a connection failure. */ }
    finally { if (playsRequest === request) playsRequest = null; }
  }
  window.addEventListener('bridge-status', function (event) {
    highlight(event.detail?.connected ? event.detail.now_playing : null);
  });
  document.addEventListener('htmx:afterSwap', function () { highlightCurrent(); refreshPlays(); });
  document.addEventListener('DOMContentLoaded', function () { highlightCurrent(); refreshCells(); }, {once: true});
  highlightCurrent();
  refreshCells();
  refreshPlays();
  setInterval(refreshCells, 2500);
  setInterval(refreshPlays, 2500);
})();

