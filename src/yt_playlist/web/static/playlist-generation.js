// A missing response can mean that YouTube saved the playlist. Keep that save
// unresolved across reloads; retrying playback must never submit another create.
const generationWindows = new Map();
const generationRequests = new WeakMap();
const generationRestoredCards = new WeakMap();
const generationWaitMs = 90000;
let generationReturnRestored = false;

function generationTuning(node) {
  const restored = generationRestoredCards.get(node);
  if (restored) return restored.tuning;
  if (!node.matches('.gen-card')) return '';
  // Home may rotate the songs on reload. Recovery follows the chosen genre
  // and vibe, so an intentional change can show a different mix in this lane.
  const recipe = JSON.parse(node.querySelector('.gen-card-body')?.dataset.recipe || '{}');
  return JSON.stringify([recipe.genre_request || null, recipe.vibe || null]);
}

function generationReturnKey(id, pathname, tuning) {
  const lane = 'tc-mix-return:' + pathname + '#' + id;
  return tuning ? lane + ':' + tuning : lane;
}

function generationRetainedReturn(node) {
  const tuning = generationTuning(node);
  const retained = JSON.parse(sessionStorage.getItem(generationReturnKey(node.id, location.pathname, tuning)));
  if (retained || !tuning) return retained;
  const earlier = JSON.parse(sessionStorage.getItem(generationReturnKey(node.id, location.pathname)));
  return earlier && generationSelectionKey(earlier.id, earlier.tracks) === generationKey(node) ? earlier : null;
}

function generationRememberReturn(context, focus = false) {
  try {
    const pathname = new URL(context.returnUrl, location.origin).pathname;
    const key = generationReturnKey(context.id, pathname, context.tuning);
    const previous = JSON.parse(sessionStorage.getItem(key));
    // A popup may only know the initial selection. Keep a destination already
    // confirmed for that selection by the save response.
    if ((previous?.recovery || previous?.saved) && !context.recovery && !context.saved &&
        (!context.launchId || previous.launchId === context.launchId) &&
        generationSelectionKey(previous.id, previous.tracks) === generationSelectionKey(context.id, context.tracks)) {
      context = previous;
    }
    sessionStorage.setItem(key, JSON.stringify(context));
    if (focus) sessionStorage.setItem('tc-mix-return', JSON.stringify({
      id: context.id, returnUrl: context.returnUrl, tuning: context.tuning
    }));
  } catch (_) {}
  return context;
}

function genReturnToMix(id, context = null, focus = true) {
  if (context && focus) context = generationRememberReturn(context, true);
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
        artist:track.artist || '', thumb:track.thumbnail || '', recordingId:track.recording_id || ''});
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
    generationRestoredCards.set(node, context);
  }
  if (!focus) return true;
  generationReturnRestored = true;
  const state = window.Alpine?.$data(node);
  if (state && 'focus' in state) state.focus = id.replace(/^gen-/, '');
  node.scrollIntoView({block: 'center', behavior: 'smooth'});
  window.focus();
  return true;
}

function generationRestoreReturn() {
  try {
    let target = JSON.parse(sessionStorage.getItem('tc-mix-return'));
    if (target && new URL(target.returnUrl, location.origin).pathname !== location.pathname) target = null;
    if (target?.tracks) {
      if (target.explicitReturn) genReturnToMix(target.id, target);
      else generationRememberReturn(target, true);
    }
    document.querySelectorAll('.gen-card[id], .starter-mix[id], .artist-first-set[id], .rt-draft[id]').forEach(node => {
      if (generationRestoredCards.has(node)) return;
      const context = generationRetainedReturn(node);
      if (context) genReturnToMix(node.id, context, !generationReturnRestored && target?.id === node.id
        && (target.tuning || '') === (context.tuning || ''));
    });
  } catch (_) {}
}
document.addEventListener('DOMContentLoaded', generationRestoreReturn);
// SPA swaps settle before Alpine initializes the new board's focus scope.
document.addEventListener('htmx:afterSettle', () => Alpine.nextTick(generationRestoreReturn));

function generationContext(node) {
  const payload = node.querySelector('.gen-card-body');
  return {id: node.id, name: payload?.dataset.name || '', tuning: generationTuning(node),
    title: node.querySelector('.gen-theme')?.textContent.trim() || payload?.dataset.name || 'Your Mix Tape',
    returnUrl: location.origin + location.pathname + location.search + '#' + node.id,
    tracks: Array.from(node.querySelectorAll('.gen-row, .rt-row'), row => ({
      video_id: row.dataset.vid, title: row.dataset.title, artist: row.dataset.artist,
      thumbnail: row.dataset.thumb, recording_id: row.dataset.recordingId || undefined
    }))};
}

function generationLaunchContext(token) {
  const record = Array.from(generationWindows.values()).find(record => record.token === token);
  return record ? {...record.context, uncertain:record.uncertain} : null;
}

function generationKey(node) {
  const tracks = Array.from(node.querySelectorAll('[data-vid]'), row => ({
    video_id: row.dataset.vid, recording_id: row.dataset.recordingId
  }));
  return generationSelectionKey(node.id, tracks);
}

function generationSelectionKey(id, tracks) {
  const identities = tracks.map(track => track.recording_id ? 'recording:' + track.recording_id : track.video_id);
  return 'tc-generation:' + id + ':' + JSON.stringify(identities.sort());
}

function generationUnresolved(node, key) {
  if (sessionStorage.getItem(key)) return true;
  // Earlier saves used upload IDs, and some preserved playlist order in the key.
  const prefix = 'tc-generation:' + node.id + ':';
  const uploads = Array.from(node.querySelectorAll('[data-vid]'), row => row.dataset.vid).sort();
  const uploadKey = prefix + JSON.stringify(uploads);
  for (let index = 0; index < sessionStorage.length; index++) {
    const previous = sessionStorage.key(index);
    if (!previous?.startsWith(prefix)) continue;
    try {
      const videos = JSON.parse(previous.slice(prefix.length));
      if (Array.isArray(videos)) {
        const previousKey = prefix + JSON.stringify(videos.sort());
        if (previousKey === key || previousKey === uploadKey) return true;
      }
    } catch (_) {}
  }
  return false;
}

function generationCard(element) {
  return element.closest('.gen-card, .starter-mix, .artist-first-set, .rt-draft');
}

function generationRestoreUnresolved() {
  document.querySelectorAll('[hx-post="/home/generate"]').forEach(button => {
    const node = generationCard(button);
    if (!node) return;
    const key = generationKey(node);
    let unresolved = false;
    try { unresolved = generationUnresolved(node, key); } catch (_) {}
    if (!unresolved) return;
    const existing = generationWindows.get(node.id);
    if (existing?.node === node && existing.key === key && node.dataset.generationPending && !existing.uncertain) return;
    let context = generationContext(node);
    try {
      const retained = generationRetainedReturn(node);
      if (retained && generationSelectionKey(retained.id, retained.tracks) === key) context = retained;
    } catch (_) {}
    const record = existing?.key === key ? existing : {
      key, context, token: crypto.randomUUID(), sent: true
    };
    record.node = node;
    generationWindows.set(node.id, record);
    generationFailure(record);
  });
}
document.addEventListener('DOMContentLoaded', generationRestoreUnresolved);
document.addEventListener('htmx:afterSettle', generationRestoreUnresolved);

function generationSettled(node) {
  if (!node.hasAttribute('data-generation-pending')) return;
  delete node.dataset.generationPending;
  // Let the save handler finish updating its card before Home swaps the board.
  queueMicrotask(() => node.dispatchEvent(new Event('mix-save-settled', {bubbles: true})));
}

function generationNote(node) {
  let note = node.querySelector('[data-generation-error]');
  if (note) return note;
  const focus = node.querySelector('.gen-focus');
  note = document.createElement('div');
  note.className = focus ? 'mix-save-status' : 'section-note';
  note.dataset.generationError = '';
  note.setAttribute('role', 'status');
  const songs = node.querySelector('.gen-recovery-tracks');
  if (focus) focus.prepend(note);
  else if (songs) songs.before(note);
  else node.append(note);
  return note;
}

function generationOwnsCard(record) {
  return record.node.isConnected && (!record.key || generationKey(record.node) === record.key);
}

function generationReceiptStatus(record) {
  const {node, context} = record;
  if (context.recovery) {
    generationSavedStatus(node, context);
    return;
  }
  const note = generationNote(node);
  const link = document.createElement('a');
  link.className = 'btn btn-sm';
  link.target = '_blank'; link.rel = 'noopener';
  if (context.saved) {
    link.href = 'https://music.youtube.com/playlist?list=' + encodeURIComponent(context.saved);
    link.textContent = 'Open saved mix';
    const count = context.added;
    note.replaceChildren(link, `${count} ${count === 1 ? 'song' : 'songs'} saved.`);
    return;
  }
  link.href = 'https://music.youtube.com/search?q=' + encodeURIComponent(context.name);
  link.textContent = 'Find mix in YouTube Music';
  note.replaceChildren(link, 'This mix may already be saved.');
  if (context.launchId) {
    const check = document.createElement('button');
    check.type = 'button'; check.className = 'btn-ghost btn-sm';
    check.disabled = !!record.checkingReceipt;
    check.textContent = record.checkingReceipt ? 'Checking saved mix…' : 'Check saved mix';
    check.onclick = () => generationCheckReceipt(record);
    note.append(check);
  }
}

function generationFailure(record) {
  record.uncertain = true;
  clearTimeout(record.timer);
  // A late failure belongs to the attempted selection even if the card has changed.
  if (generationOwnsCard(record)) {
    generationSettled(record.node);
    const focused = record.node.classList.contains('is-focused');
    if (record.context.launchId) record.context = generationRememberReturn(record.context, focused);
    generationReceiptStatus(record);
    record.node.querySelectorAll('[hx-post="/home/generate"], .gen-refresh, .gen-rm').forEach(button => {
      button.disabled = true;
    });
    if (record.context.launchId) {
      if (record.tab && !record.tab.closed && focused) window.focus();
      generationCloseWindow(record);
      if (record.token === record.context.launchId && !record.checkedReceipt &&
          !record.context.saved && !record.context.recovery) generationCheckReceipt(record);
      return;
    }
  }
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

async function generationCheckReceipt(record) {
  if (record.checkingReceipt || !generationOwnsCard(record)) return;
  record.checkedReceipt = record.checkingReceipt = true;
  generationReceiptStatus(record);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch('/home/generate/reconcile', {method: 'POST', signal: controller.signal,
      body: new URLSearchParams({launch_id: record.context.launchId})});
    const receipt = response.ok && await response.json();
    if (!receipt || !generationOwnsCard(record)) return;
    if (receipt.saved) Object.assign(record.context, {saved: receipt.saved, added: receipt.added});
    if (receipt.recovery) Object.assign(record.context, {recovery: receipt.recovery, recoveryTracks: receipt.tracks});
    generationRememberReturn(record.context);
  } catch (error) { console.debug('saved mix receipt check deferred', error); }
  finally {
    clearTimeout(timer);
    record.checkingReceipt = false;
    if (generationOwnsCard(record)) generationReceiptStatus(record);
  }
}

function generationCloseWindow(record) {
  try { if (record.tab && !record.tab.closed) record.tab.close(); } catch (_) {}
  if (record.popupUrl) URL.revokeObjectURL(record.popupUrl);
  if (window.__ytTab === record.tab) window.__ytTab = null;
}

function generationReturnFromLaunch(token) {
  const record = Array.from(generationWindows.values()).find(item => item.token === token);
  if (!record?.context.launchId || !generationOwnsCard(record)) return false;
  generationFailure(record);
  return true;
}

function generationSavedStatus(node, context) {
  const recovery = context.recovery;
  const attempted = context.recoveryTracks || context.tracks;
  const replacements = new Map(attempted.filter(track => track.recording_id)
    .map(track => [String(track.recording_id), track.video_id]));
  const note = generationNote(node);
  const summary = node.querySelector('[data-generation-summary]');
  if (summary) summary.textContent = recovery.uncertain.length ? 'Some songs are still unconfirmed.' :
    recovery.rejected.length || recovery.unattempted.length ? 'Some selected songs were not saved.' : 'Your selected songs are saved.';
  const labels = {confirmed: 'Saved', uncertain: 'Unconfirmed', rejected: 'Not accepted', unattempted: 'Not saved'};
  const counts = [];
  const showTrackStatus = Object.keys(labels).filter(state => recovery[state].length).length > 1;
  node.querySelectorAll('[data-generation-progress]').forEach(progress => progress.remove());
  for (const [state, label] of Object.entries(labels)) {
    const videos = recovery[state];
    if (videos.length) {
      const songs = videos.length === 1 ? 'song' : 'songs';
      const status = state === 'uncertain' ? 'still to check in YouTube Music' : label.toLowerCase();
      counts.push(`${videos.length} ${songs} ${status}`);
    }
    if (showTrackStatus) node.querySelectorAll('.gen-row, [data-generation-video]').forEach(row => {
      const video = replacements.get(row.dataset.recordingId) || row.dataset.vid || row.dataset.generationVideo;
      if (!videos.includes(video)) return;
      const progress = document.createElement('span');
      progress.className = 'gen-artist';
      progress.dataset.generationProgress = '';
      progress.textContent = label;
      (row.querySelector('.gen-meta, .starter-meta') || row).append(progress);
    });
  }
  const link = document.createElement('a');
  link.className = 'btn btn-sm';
  link.href = 'https://music.youtube.com/playlist?list=' + encodeURIComponent(recovery.playlist);
  link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Open saved mix';
  note.replaceChildren(link, counts.join(' · ') + '.');
  if (!recovery.uncertain.length && !recovery.rejected.length && !recovery.unattempted.length) return;
  const check = document.createElement('button');
  check.type = 'button'; check.className = 'btn-ghost btn-sm'; check.textContent = 'Check saved mix';
  check.onclick = async () => {
    check.disabled = true; check.textContent = 'Checking saved mix…';
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/home/generate/reconcile', {method: 'POST', signal: controller.signal,
        body: new URLSearchParams({tracks: JSON.stringify(attempted), recovery: JSON.stringify(recovery)})});
      const updated = response.ok && await response.json();
      if (updated) {
        context.recovery = updated;
        if (context.id && context.returnUrl) generationRememberReturn(context);
      }
    } catch (error) { console.debug('saved mix check deferred', error); }
    finally {
      clearTimeout(timer);
      generationSavedStatus(node, context);
    }
  };
  note.append(check);
}

// Home and Road Trip share the generation window. Save ownership is attached to
// the individual request so a later click cannot close another mix's window.
function genOpenYT(id) {
  const node = document.getElementById(id);
  const key = generationKey(node);
  const existing = generationWindows.get(id);
  if (existing?.sent && existing.key === key) {
    existing.node = node;
    generationFailure(existing);
    return;
  }
  let unresolved = false;
  try { unresolved = generationUnresolved(node, key); } catch (_) {}
  const record = {node, key, context: generationContext(node), token: crypto.randomUUID(), sent: unresolved, uncertain: unresolved};
  if (!unresolved && node.matches('.gen-card')) record.context.launchId = record.token;
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
  if (event.detail.path !== '/home/generate' && !/^\/road_trip\/draft\/\d+\/save$/.test(event.detail.path)) return;
  const record = Array.from(generationWindows.values()).find(item => item.node.contains(event.detail.elt));
  if (record) event.detail.parameters.launch_id = record.token;
});

// HTMX delivers this before following HX-Redirect, without replacing the mix.
document.addEventListener('generated-playlist-saved', event => {
  // Keep the spoke loader open while the destination is still loading.
  const complete = () => {
    window.removeEventListener('pagehide', complete);
    window.removeEventListener('app-page-changed', complete);
    genComplete(event.detail.launch_id, event.detail.playlist, false, false, true);
  };
  window.addEventListener('pagehide', complete, {once: true});
  window.addEventListener('app-page-changed', complete, {once: true});
});

document.addEventListener('htmx:beforeRequest', event => {
  if (event.detail.requestConfig?.path !== '/home/generate') return;
  const node = generationCard(event.detail.elt);
  // Check durable unresolved identity independently of click handlers and old
  // DOM ownership. Reconnect can replace the card before its settle callbacks.
  if (node) {
    let unresolved = false;
    try { unresolved = generationUnresolved(node, generationKey(node)); } catch (_) {}
    if (unresolved) {
      event.preventDefault();
      generationRestoreUnresolved();
      return;
    }
  }
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
  if (event.detail.xhr.getResponseHeader('X-Mix-Recovery') === '1') {
    const {recovery, tracks} = JSON.parse(event.detail.xhr.responseText);
    delete record.context.launchId;
    genComplete(record.token, null, true, true, false, recovery, tracks);
    generationFailure(record);
    // HTMX restores disabled elements after dispatching its lifecycle events.
    setTimeout(() => generationFailure(record), 0);
  } else if (event.detail.failed || event.detail.xhr.status === 0) {
    clearTimeout(record.timer);
    generationFailure(record);
    // HTMX restores disabled elements after dispatching its lifecycle events.
    setTimeout(() => generationFailure(record), 0);
  }
});

function genComplete(token, playlist, error, uncertain = false, redirecting = false, recovery = null, attempted = null) {
  const record = Array.from(generationWindows.values()).find(item => item.token === token);
  if (record) {
    clearTimeout(record.timer);
    generationSettled(record.node);
    if (uncertain) {
      if (recovery) {
        record.context.recovery = recovery;
        record.context.recoveryTracks = attempted;
      }
      generationRememberReturn(record.context, true);
      generationRestoredCards.set(record.node, record.context);
    } else {
      try { sessionStorage.removeItem(record.key); } catch (_) {}
      try { sessionStorage.removeItem('tc-mix-launch:' + token); } catch (_) {}
      try {
        const pathname = new URL(record.context.returnUrl, location.origin).pathname;
        const key = generationReturnKey(record.node.id, pathname, record.context.tuning);
        const previous = JSON.parse(sessionStorage.getItem(key));
        if (previous && generationSelectionKey(previous.id, previous.tracks) === record.key) {
          sessionStorage.removeItem(key);
          const target = JSON.parse(sessionStorage.getItem('tc-mix-return'));
          if (target?.returnUrl === record.context.returnUrl && (target.tuning || '') === record.context.tuning) {
            sessionStorage.removeItem('tc-mix-return');
          }
        }
      } catch (_) {}
    }
    generationCloseWindow(record);
    if (generationWindows.get(record.node.id) === record) generationWindows.delete(record.node.id);
  } else if (!token && !Array.from(generationWindows.values()).some(item => item.tab === window.__ytTab)) {
    // Clusters also uses this receipt with its untokened loading window.
    // An unmatched mix token must never close another selection's popup.
    try { if (window.__ytTab && !window.__ytTab.closed) window.__ytTab.close(); } catch (_) {}
    window.__ytTab = null;
  }
  if (playlist && !error) {
    try { sessionStorage.setItem('tc-generated-playback', JSON.stringify({playlist, at: Date.now(), redirecting})); } catch (_) {}
    window.dispatchEvent(new Event('generated-playback-pending'));
  }
  return record?.context;
}

