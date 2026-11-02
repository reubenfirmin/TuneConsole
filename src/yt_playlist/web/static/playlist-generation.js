// A missing response can mean that YouTube saved the playlist. Keep that save
// unresolved across reloads; retrying playback must never submit another create.
const generationWindows = new Map();
const generationRequests = new WeakMap();
const generationWaitMs = 90000;
let generationReturnRestored = false;

function genReturnToMix(id, context = null) {
  if (context) {
    try { sessionStorage.setItem('tc-mix-return', JSON.stringify(context)); } catch (_) {}
  }
  const node = document.getElementById(id);
  if (!node) return false;
  if (context) {
    const list = node.querySelector('.gen-list, .starter-tracks');
    if (!list) return false;
    const rows = Array.from(node.querySelectorAll('.gen-row'));
    const restored = context.tracks.map((track, index) => {
      const existing = rows.find(row => row.dataset.vid === track.video_id);
      if (existing) rows.splice(rows.indexOf(existing), 1);
      const row = existing || document.createElement('li');
      row.className = 'gen-row' + (list.matches('.starter-tracks') ? ' starter-track' : '');
      Object.assign(row.dataset, {vid:track.video_id, title:track.title || '',
        artist:track.artist || '', thumb:track.thumbnail || ''});
      row.replaceChildren();
      const number = document.createElement('span');
      number.className = list.matches('.starter-tracks') ? 'starter-number' : 'gen-index';
      number.textContent = index + 1;
      const image = document.createElement('img'); image.alt = ''; image.referrerPolicy = 'no-referrer';
      image.src = track.thumbnail || 'https://i.ytimg.com/vi/' + encodeURIComponent(track.video_id) + '/hqdefault.jpg';
      image.onerror = () => { image.hidden = true; };
      const starter = list.matches('.starter-tracks');
      const art = document.createElement('span'); art.className = starter ? 'starter-thumb' : 'gen-th'; art.append(image);
      const song = document.createElement('a'); song.className = 'gen-title';
      song.textContent = track.title || ''; song.href = 'https://music.youtube.com/watch?v=' + encodeURIComponent(track.video_id);
      song.target = '_blank'; song.rel = 'noopener';
      const artist = document.createElement('span'); artist.className = 'gen-artist'; artist.textContent = track.artist || '';
      const meta = document.createElement('span'); meta.className = starter ? 'starter-meta' : 'gen-meta'; meta.append(song, artist);
      if (starter) {
        const content = document.createElement('span'); content.className = 'starter-song'; content.append(art, meta);
        row.append(number, content);
      } else row.append(number, art, meta);
      return row;
    });
    list.replaceChildren(...restored);
    try { sessionStorage.setItem(generationKey(node), 'uncertain'); } catch (_) {}
    const poster = node.querySelector('.gen-poster');
    if (poster) poster.replaceChildren(...restored.slice(0, 4).map(row => {
      const tile = document.createElement('span'); tile.className = 'gen-poster-cell';
      tile.append(row.querySelector('img').cloneNode()); return tile;
    }));
    const payload = node.querySelector('.gen-card-body');
    if (payload) payload.dataset.name = context.name;
    const title = node.querySelector('.gen-theme');
    if (title) title.textContent = context.title;
    const count = node.querySelector('.gen-sheet-head strong');
    if (count) count.textContent = context.tracks.length;
    node.querySelectorAll('.gen-refresh').forEach(button => { button.disabled = true; });
    // These are the attempted choices, not a newly sampled preview. Keep its
    // existing unresolved-create guard and provide direct song playback.
    generationFailure({node, context, uncertain:true});
    generationReturnRestored = true;
  }
  const state = window.Alpine?.$data(node);
  if (state && 'focus' in state) state.focus = id.replace(/^gen-/, '');
  node.scrollIntoView({block: 'center', behavior: 'smooth'});
  window.focus();
  return true;
}

function generationRestoreReturn() {
  if (generationReturnRestored) return;
  try {
    const context = JSON.parse(sessionStorage.getItem('tc-mix-return'));
    if (context && new URL(context.returnUrl, location.origin).pathname === location.pathname) genReturnToMix(context.id, context);
  } catch (_) {}
}
document.addEventListener('DOMContentLoaded', generationRestoreReturn);
document.addEventListener('htmx:afterSettle', generationRestoreReturn);

function generationContext(node) {
  const payload = node.querySelector('.gen-card-body');
  return {id: node.id, name: payload?.dataset.name || '',
    title: node.querySelector('.gen-theme')?.textContent.trim() || payload?.dataset.name || 'Your Mix Tape',
    returnUrl: location.origin + location.pathname + location.search + '#' + node.id,
    tracks: Array.from(node.querySelectorAll('.gen-row, .rt-row'), row => ({
      video_id: row.dataset.vid, title: row.dataset.title, artist: row.dataset.artist,
      thumbnail: row.dataset.thumb
    }))};
}

function generationLaunchContext(token) {
  const record = Array.from(generationWindows.values()).find(record => record.token === token);
  return record ? {...record.context, uncertain:record.uncertain} : null;
}

function generationKey(node) {
  const videos = Array.from(node.querySelectorAll('[data-vid]')).map(row => row.dataset.vid).sort();
  return 'tc-generation:' + node.id + ':' + JSON.stringify(videos);
}

function generationUnresolved(node, key) {
  if (sessionStorage.getItem(key)) return true;
  // Preserve unresolved saves recorded by the earlier order-sensitive key.
  const prefix = 'tc-generation:' + node.id + ':';
  for (let index = 0; index < sessionStorage.length; index++) {
    const previous = sessionStorage.key(index);
    if (!previous?.startsWith(prefix)) continue;
    try {
      const videos = JSON.parse(previous.slice(prefix.length));
      if (Array.isArray(videos) && prefix + JSON.stringify(videos.sort()) === key) return true;
    } catch (_) {}
  }
  return false;
}

function generationFailure(record) {
  delete record.node.dataset.generationPending;
  record.uncertain = true;
  const detail = 'We couldn’t confirm playback of “' + record.context.title +
    '”. This mix may already be in your YouTube Music library.';
  let note = record.node.querySelector('[data-generation-error]');
  if (!note) {
    note = document.createElement('p');
    note.className = 'section-note';
    note.dataset.generationError = '';
    note.setAttribute('role', 'alert');
    record.node.append(note);
  }
  note.replaceChildren(document.createTextNode(detail + ' '));
  const link = document.createElement('a');
  link.href = 'https://music.youtube.com/search?q=' + encodeURIComponent(record.context.name);
  link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Find this mix on YouTube Music';
  note.append(link);
  record.node.querySelectorAll('[hx-post="/home/generate"]').forEach(button => { button.disabled = true; });
  try {
    const tab = record.tab;
    if (tab && !tab.closed) {
      // The committed document owns its handlers so they survive this tab
      // reloading, navigating away or closing. A not-yet-loaded document reads
      // record.uncertain when its own script starts.
      tab.postMessage({type: 'generation-failed', token: record.token}, location.origin);
    }
  } catch (_) {}
}

// Home and Road Trip share the generation window. Save ownership is attached to
// the individual request so a later click cannot close another mix's window.
function genOpenYT(id) {
  const node = document.getElementById(id);
  const existing = generationWindows.get(id);
  if (existing?.sent) { generationFailure(existing); return; }
  const key = generationKey(node);
  let unresolved = false;
  try { unresolved = generationUnresolved(node, key); } catch (_) {}
  const record = {node, key, context: generationContext(node), token: crypto.randomUUID(), sent: unresolved, uncertain: unresolved};
  generationWindows.set(id, record);
  if (unresolved) { generationFailure(record); return; }
  try {
    const thumbs = Array.from(node.querySelectorAll('.gen-row, .rt-row'))
      .map((row) => row.dataset.thumb).filter(Boolean).slice(0, 16);
    localStorage.setItem('tc_gen_thumbs', JSON.stringify(thumbs));
    sessionStorage.setItem('tc-mix-launch:' + record.token, JSON.stringify(record.context));
  } catch (_) {}
  try {
    const tab = record.tab = window.__ytTab = window.open('', '_blank');
    if (tab) {
      // The local page can be waiting on a busy or unavailable backend. Paint
      // feedback without making this window depend on another HTTP request.
      renderMixLaunch(tab.document, record.context);
      // Commit a local document so Chromium fetches artwork, without another
      // backend request. Its own deadline also survives an opener reload.
      const context = JSON.stringify(record.context).replace(/</g, '\\u003c');
      const token = JSON.stringify(record.token);
      const script = renderMixLaunch.toString() + '\n' + startMixLaunch.toString() +
        '\nstartMixLaunch(' + context + ',' + token + ');';
      const loader = document.getElementById('mix-launch-loader').innerHTML;
      record.popupUrl = URL.createObjectURL(new Blob([
        '<!doctype html><html class="gen-page"><head><meta charset="utf-8">' +
        '<meta name="viewport" content="width=device-width,initial-scale=1">' +
        '<base href="' + location.origin + '/"></head><body>' + loader +
        '<script>' + script + '</script></body></html>'
      ], {type:'text/html'}));
      tab.location.replace(record.popupUrl);
    }
  } catch (_) {}
}

document.addEventListener('htmx:configRequest', event => {
  if (event.detail.path !== '/home/generate') return;
  const record = Array.from(generationWindows.values()).find(item => item.node.contains(event.detail.elt));
  if (record) event.detail.parameters.launch_id = record.token;
});

// HTMX delivers this before following HX-Redirect, without replacing the mix.
document.addEventListener('generated-playlist-saved', event => {
  // Keep the spoke loader open while the destination is still loading.
  window.addEventListener('pagehide', () => {
    genComplete(event.detail.launch_id, event.detail.playlist, false, false, true);
  }, {once: true});
});

document.addEventListener('htmx:beforeRequest', event => {
  if (event.detail.requestConfig?.path !== '/home/generate') return;
  const record = Array.from(generationWindows.values()).find(item => item.node.contains(event.detail.elt));
  if (!record) return;
  if (record.sent) { event.preventDefault(); generationFailure(record); return; }
  record.sent = true;
  record.node.dataset.generationPending = 'true';
  try { sessionStorage.setItem(record.key, record.token); } catch (_) {}
  generationRequests.set(event.detail.xhr, record);
  record.timer = setTimeout(() => generationFailure(record), generationWaitMs);
});

document.addEventListener('htmx:afterRequest', event => {
  const record = generationRequests.get(event.detail.xhr);
  if (!record) return;
  if (event.detail.failed || event.detail.xhr.status === 0) {
    clearTimeout(record.timer);
    generationFailure(record);
    // HTMX restores disabled elements after dispatching its lifecycle events.
    setTimeout(() => generationFailure(record), 0);
  }
});

function genComplete(token, playlist, error, uncertain = false, redirecting = false) {
  const record = Array.from(generationWindows.values()).find(item => item.token === token);
  if (record) {
    clearTimeout(record.timer);
    delete record.node.dataset.generationPending;
    if (uncertain) {
      try { sessionStorage.setItem('tc-mix-return', JSON.stringify(record.context)); } catch (_) {}
    } else {
      try { sessionStorage.removeItem(record.key); } catch (_) {}
      try { sessionStorage.removeItem('tc-mix-launch:' + token); } catch (_) {}
      try { sessionStorage.removeItem('tc-mix-return'); } catch (_) {}
    }
    try { if (record.tab && !record.tab.closed) record.tab.close(); } catch (_) {}
    if (record.popupUrl) URL.revokeObjectURL(record.popupUrl);
    generationWindows.delete(record.node.id);
  } else {
    try { if (window.__ytTab && !window.__ytTab.closed) window.__ytTab.close(); } catch (_) {}
  }
  window.__ytTab = null;
  if (playlist && !error) {
    try { sessionStorage.setItem('tc-generated-playback', JSON.stringify({playlist, at: Date.now(), redirecting})); } catch (_) {}
    window.dispatchEvent(new Event('generated-playback-pending'));
  }
}

