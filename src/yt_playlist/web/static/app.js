// Keep --topbar-h in sync with the real navbar height so sticky table headers pin right below it.
function syncTopbarH() {
  const tb = document.querySelector('.topbar');
  if (tb) document.documentElement.style.setProperty('--topbar-h', tb.offsetHeight + 'px');
}
window.addEventListener('DOMContentLoaded', syncTopbarH);
window.addEventListener('resize', syncTopbarH);

// htmx: a 422 carries an OOB error toast. By default htmx won't process a 4xx body,
// so opt this status in — the server sets `HX-Reswap: none` to keep the primary
// target untouched while the OOB toast still lands in #toasts. Bind on `document`
// (not document.body): app.js loads in <head>, before <body> exists.
document.addEventListener('htmx:beforeSwap', (e) => {
  if (e.detail.xhr.status === 422) { e.detail.shouldSwap = true; e.detail.isError = false; }
});

// Alpine component factories for the various pages (loaded globally via base.html).
function emptyRow() {
  return {
    state: 'idle', err: '',
    async del(id) {
      if (this.state === 'deleting') return;
      this.state = 'deleting'; this.err = '';
      try {
        const fd = new FormData(); fd.append('playlist', id);
        const r = await fetch('/playlist/delete-empty', { method: 'POST', body: fd });
        const j = await r.json();
        if (j.ok) { this.state = 'gone'; } else { this.state = 'idle'; this.err = j.error || 'failed'; }
      } catch (e) { this.state = 'idle'; this.err = String(e); }
    },
  };
}
function hideRow() {
  return {
    state: 'idle', open: false, mx: 0, my: 0,
    async hide(a, b) {
      if (this.state === 'hiding') return;
      this.state = 'hiding';
      try {
        const fd = new FormData(); fd.append('a', a); fd.append('b', b);
        const r = await fetch('/overlaps/suppress', { method: 'POST', body: fd });
        if ((await r.json()).ok) { this.state = 'gone'; } else { this.state = 'idle'; }
      } catch (e) { this.state = 'idle'; }
    },
  };
}
function saveAlbum(saved) {
  // Save/unsave a YouTube album to your library, then reload so both album tables refresh.
  return {
    saved: !!saved, busy: false,
    label() { return this.busy ? '…' : (this.saved ? 'Unsave' : 'Save'); },
    async toggle(browseId) {
      if (this.busy) return;
      this.busy = true;
      const url = this.saved ? '/collection/unsave-album' : '/collection/save-album';
      try {
        const fd = new FormData(); fd.append('browse_id', browseId);
        const j = await (await fetch(url, { method: 'POST', body: fd })).json();
        if (j.ok) { location.reload(); return; }
      } catch (e) {}
      this.busy = false;
    },
  };
}
function rowSort(pid) {
  // Generic click-to-sort for a static-row table; reorders <tr class="srow"> by data-<key>.
  // Numeric when both values parse as numbers, else locale string compare.
  // Also hosts the per-row "⋯" menu and the "find alternate versions" flow for the playlist view.
  return {
    pid: pid, key: '', dir: 1,
    openMenu: null,                                   // video_id whose ⋯ menu is open
    // alternate-versions modal
    altOpen: false, altLoading: false, altErr: '', altTitle: '', altResults: [], altSel: {},
    sortBy(k) {
      if (this.key === k) { this.dir = -this.dir; } else { this.key = k; this.dir = 1; }
      const tb = this.$refs.body;
      if (!tb) return;
      Array.from(tb.querySelectorAll('tr.srow'))
        .sort((x, y) => {
          const a = x.dataset[k] || '', b = y.dataset[k] || '';
          const na = parseFloat(a), nb = parseFloat(b);
          const numeric = a !== '' && b !== '' && !isNaN(na) && !isNaN(nb);
          return (numeric ? na - nb : a.localeCompare(b)) * this.dir;
        })
        .forEach(r => tb.appendChild(r));
    },
    ind(k) { return this.key === k ? (this.dir === 1 ? ' ▲' : ' ▼') : ''; },

    async findAlternates(vid, title) {
      this.openMenu = null;
      this.altOpen = true; this.altLoading = true; this.altErr = '';
      this.altResults = []; this.altSel = {}; this.altTitle = title;
      try {
        const r = await fetch(`/playlist/${this.pid}/alternates?video_id=${encodeURIComponent(vid)}`);
        const j = await r.json();
        if (!j.ok) { this.altErr = j.error || 'search failed'; }
        else { this.altResults = j.results; if (!j.results.length) this.altErr = 'No other versions found.'; }
      } catch (e) { this.altErr = 'network error'; }
      this.altLoading = false;
    },
    toggleAlt(vid) {
      const s = { ...this.altSel };
      if (s[vid]) delete s[vid]; else s[vid] = true;
      this.altSel = s;
    },
    altCount() { return Object.keys(this.altSel).length; },
    async addAlternates() {
      const chosen = this.altResults.filter(r => this.altSel[r.videoId]);
      if (!chosen.length) return;
      this.altLoading = true; this.altErr = '';
      try {
        const r = await fetch(`/playlist/${this.pid}/add-tracks`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tracks: chosen }),
        });
        const j = await r.json();
        if (!j.ok) { this.altErr = j.error || 'add failed'; this.altLoading = false; return; }
        location.reload();                            // new tracks drop into the table
      } catch (e) { this.altErr = 'network error'; this.altLoading = false; }
    },
    fmtDur(s) { return s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : ''; },

    // remove-track confirmation modal
    rmOpen: false, rmBusy: false, rmErr: '', rmVid: '', rmTitle: '',
    removeTrack(vid, title) {
      this.openMenu = null;
      this.rmVid = vid; this.rmTitle = title; this.rmErr = ''; this.rmOpen = true;
    },
    async confirmRemove() {
      this.rmBusy = true; this.rmErr = '';
      try {
        const r = await fetch(`/playlist/${this.pid}/remove-track`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ video_id: this.rmVid }),
        });
        const j = await r.json();
        if (!j.ok) { this.rmErr = j.error || 'remove failed'; this.rmBusy = false; return; }
        location.reload();
      } catch (e) { this.rmErr = 'network error'; this.rmBusy = false; }
    },

    // --- drag-and-drop reorder via SortableJS (floating drag clone + ghost placeholder) ---
    initSortable() {
      const tb = this.$refs.body;
      if (!tb || typeof Sortable === 'undefined') return;
      Sortable.create(tb, {
        handle: '.drag-handle',
        draggable: 'tr.srow',
        animation: 160,
        // Native drag image (a faithful, full-width snapshot of the row) floats under the cursor —
        // a fallback clone would detach the <tr> from the table and collapse its columns.
        ghostClass: 'srow-ghost',            // the placeholder shown at the insert point
        chosenClass: 'srow-chosen',
        onEnd: (e) => {
          this.renumber();
          if (e.oldIndex === e.newIndex) return;
          const rows = tb.querySelectorAll('tr.srow');
          const moved = e.item.dataset.vid;
          const next = e.item.nextElementSibling;
          const beforeVid = next && next.classList.contains('srow') ? next.dataset.vid : '';
          fetch(`/playlist/${this.pid}/reorder`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ video_id: moved, before_video_id: beforeVid }),
          }).then(r => r.json()).then(j => { if (!j.ok) location.reload(); })
            .catch(() => location.reload());
        },
      });
    },
    renumber() {
      this.$refs.body.querySelectorAll('tr.srow .rownum').forEach((el, i) => { el.textContent = i + 1; });
    },

    // --- click-to-edit genre with a custom, constrained autosuggest dropdown ---
    editVid: null, genreList: [], gSuggest: [], gSel: -1,
    _loadGenres() {
      if (!this.genreList.length)
        this.genreList = Array.from(document.querySelectorAll('#genrelist option')).map(o => o.value);
    },
    filterGenres(val) {
      this._loadGenres();
      const q = (val || '').trim().toLowerCase();
      const all = this.genreList;
      this.gSuggest = (q ? all.filter(g => g.toLowerCase().includes(q)) : all).slice(0, 8);
      this.gSel = -1;
    },
    moveGenreSel(d) {
      if (!this.gSuggest.length) return;
      this.gSel = (this.gSel + d + this.gSuggest.length) % this.gSuggest.length;
    },
    startEditGenre(vid) {
      this._loadGenres();
      this.editVid = vid; this.gSuggest = []; this.gSel = -1;
      this.$nextTick(() => {
        const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(vid)}"]`);
        const inp = tr && tr.querySelector('.ginput');
        const tag = tr && tr.querySelector('.gtag');
        if (inp) { inp.value = tag ? tag.textContent.trim() : ''; inp.focus(); inp.select(); this.filterGenres(inp.value); }
      });
    },
    async saveGenre(vid, value) {
      if (this.editVid !== vid) return;          // ignore the trailing blur after enter/escape
      this.editVid = null; this.gSuggest = []; this.gSel = -1;
      const genre = (value || '').trim();
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(vid)}"]`);
      if (tr) {                                   // optimistic update
        const disp = tr.querySelector('.gdisplay');
        if (disp) disp.innerHTML = genreChip(genre);
        tr.dataset.genre = genre.toLowerCase();
      }
      try {
        await fetch(`/playlist/${this.pid}/track-genre`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ video_id: vid, genre }),
        });
      } catch (e) { /* optimistic UI already applied; a reload would resync if needed */ }
    },

    // --- click-to-edit year ---
    editYearVid: null,
    startEditYear(vid) {
      this.editYearVid = vid;
      this.$nextTick(() => {
        const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(vid)}"]`);
        const inp = tr && tr.querySelector('.yinput');
        const disp = tr && tr.querySelector('.ydisplay');
        if (inp) { inp.value = (disp ? disp.textContent : '').trim().replace(/\D/g, ''); inp.focus(); inp.select(); }
      });
    },
    async saveYear(vid, value) {
      if (this.editYearVid !== vid) return;
      this.editYearVid = null;
      const year = (value || '').trim();
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(vid)}"]`);
      if (tr) {
        const disp = tr.querySelector('.ydisplay');
        if (disp) disp.innerHTML = year ? escapeHtml(year) : '<span class="muted ghint">＋</span>';
        tr.dataset.year = year || 0;
      }
      try {
        await fetch(`/playlist/${this.pid}/track-year`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ video_id: vid, year }),
        });
      } catch (e) { /* optimistic UI already applied */ }
    },
  };
}
function overlapSort() {
  // Client-side sort of the overlaps table by reordering the per-row <tbody> nodes
  // (which preserves each row's Alpine state — pie menu, hide animation, etc.).
  return {
    key: 'shared', dir: -1,   // default: most-overlapping first
    tail: 0, tailMax: 0, _below: [], confirmOpen: false,
    start() { this.apply(); },
    sortBy(k) {
      if (this.key === k) { this.dir = -this.dir; }
      else { this.key = k; this.dir = (k === 'shared') ? -1 : 1; }
      this.apply();
    },
    apply() {
      const tbl = this.$refs.tbl;
      if (!tbl) return;
      const rows = Array.from(tbl.querySelectorAll('tbody.ov-row'));
      rows.sort((x, y) => {
        if (this.key === 'shared') return (+x.dataset.shared - +y.dataset.shared) * this.dir;
        return (x.dataset[this.key] || '').localeCompare(y.dataset[this.key] || '') * this.dir;
      });
      rows.forEach(r => tbl.appendChild(r));   // re-append in order; moving keeps node state
    },
    // Anchored to a TABLE ROW (not the scroll position): hide this row + every row after it
    // in the current sort order. The control lives on each row, so the cut point is a row.
    dismissFromRow(rowEl) {
      const tbl = this.$refs.tbl;
      if (!tbl || !rowEl) return;
      const rows = Array.from(tbl.querySelectorAll('tbody.ov-row')).filter(r => r.style.display !== 'none');
      const idx = rows.indexOf(rowEl);
      if (idx < 0) return;
      this._below = rows.slice(idx);
      this.tail = this._below.length;
      this.tailMax = this._below.reduce((m, r) => Math.max(m, +r.dataset.shared), 0);
      this.confirmOpen = true;
    },
    async confirmDismiss() {
      this.confirmOpen = false;
      if (!this._below.length) return;
      const pairs = this._below.map(r => [r.dataset.ay, r.dataset.by]);
      try {
        const fd = new FormData(); fd.append('pairs', JSON.stringify(pairs));
        await fetch('/overlaps/suppress-many', { method: 'POST', body: fd });
      } catch (e) {}
      location.hash = 'overlaps'; location.reload();
    },
    ind(k) { return this.key === k ? (this.dir === 1 ? ' ▲' : ' ▼') : ''; },
  };
}
function playlistsTab(rows) {
  return {
    rows, sel: {}, sortKey: 'title', sortDir: 1, split: false, busy: false,
    groupModal: false, groupName: '', delModal: false,
    init() {
      // remember view preferences across reloads (the tab reloads after group/delete)
      try {
        this.split = localStorage.getItem('pl.split') === '1';
        this.sortKey = localStorage.getItem('pl.sortKey') || 'title';
        this.sortDir = +localStorage.getItem('pl.sortDir') || 1;
      } catch (e) {}
      this.$watch('split', v => { try { localStorage.setItem('pl.split', v ? '1' : '0'); } catch (e) {} });
      this.$watch('sortKey', v => { try { localStorage.setItem('pl.sortKey', v); } catch (e) {} });
      this.$watch('sortDir', v => { try { localStorage.setItem('pl.sortDir', v); } catch (e) {} });
    },
    selected() { return this.rows.filter(r => this.sel[r.id]); },
    count() { return this.selected().length; },
    toggle(id) { this.sel[id] = !this.sel[id]; },
    sortBy(key) {
      if (this.sortKey === key) { this.sortDir = -this.sortDir; }
      else { this.sortKey = key; this.sortDir = ['count', 'listens', 'last'].includes(key) ? -1 : 1; }
    },
    ind(key) { return this.sortKey === key ? (this.sortDir === 1 ? ' ▲' : ' ▼') : ''; },
    cmp(a, b) {
      const k = this.sortKey, numeric = (k === 'count' || k === 'listens' || k === 'last');
      const r = numeric ? ((a[k] || 0) - (b[k] || 0))
                        : String(a[k] || '').localeCompare(String(b[k] || ''));
      return (r ? r * this.sortDir : a.title.localeCompare(b.title));   // stable tiebreak by title
    },
    sorted() { return [...this.rows].sort((a, b) => this.cmp(a, b)); },
    sections() {
      // when split, partition by group name (Ungrouped last); else one "" section
      if (!this.split) return [{ name: '', rows: this.sorted() }];
      const m = {};
      this.sorted().forEach(r => { const g = r.group || 'Ungrouped'; (m[g] = m[g] || []).push(r); });
      return Object.keys(m)
        .sort((a, b) => a === 'Ungrouped' ? 1 : b === 'Ungrouped' ? -1 : a.localeCompare(b))
        .map(n => ({ name: n, rows: m[n] }));
    },
    selectAll(on) { this.rows.forEach(r => { this.sel[r.id] = on; }); },
    fmtLast(ts) {
      if (!ts) return '—';
      const days = Math.floor((Date.now() / 1000 - ts) / 86400);
      if (days <= 0) return 'today';
      if (days === 1) return 'yesterday';
      if (days < 30) return days + 'd ago';
      if (days < 365) return Math.floor(days / 30) + 'mo ago';
      return Math.floor(days / 365) + 'y ago';
    },
    merge() {
      if (this.count() < 2) return;
      location.href = '/merge?ids=' + this.selected().map(r => r.id).join(',') + '&return=/';
    },
    async _post(url, data) {
      const fd = new FormData();
      Object.entries(data).forEach(([k, v]) => fd.append(k, v));
      const r = await fetch(url, { method: 'POST', body: fd });
      return r.json();
    },
    copyModal: false, copyName: '', copyIds: [],
    openCopy() {
      const sel = this.selected();
      if (!sel.length) return;
      this.copyIds = sel.map(r => r.id);
      // single -> "Title (copy)"; multiple -> a copy+merge, prefilled with the joined names
      this.copyName = sel.length === 1 ? sel[0].title + ' (copy)' : sel.map(r => r.title).join(' + ');
      this.copyModal = true;
    },
    async doCopy() {
      this.copyModal = false;
      await this._post('/playlists/copy', { ids: this.copyIds.join(','), name: this.copyName });
      location.reload();                      // new playlist drops into the table
    },
    openGroup() { if (this.count()) { this.groupName = ''; this.groupModal = true; } },
    async doGroup() {
      this.groupModal = false;
      await this._post('/playlists/group', { ids: this.selected().map(r => r.id).join(','), name: this.groupName });
      location.reload();
    },
    async doDelete() {
      this.delModal = false;
      await this._post('/playlists/delete', { ids: this.selected().map(r => r.id).join(',') });
      location.reload();
    },
  };
}
function moveTab(fromId, toId) {
  // Move page: the from/to identity selection and the "two different identities" gate.
  // The copy/move actions themselves are htmx (see _partials/move_row.html).
  return {
    from: fromId, to: toId,
    canMove() { return this.from != null && this.to != null && this.from !== this.to; },
  };
}
function _reloadOverlaps() { location.hash = 'overlaps'; location.reload(); }
async function _ignoreExcept(ytm, a, b) {
  const fd = new FormData(); fd.append('ytm', ytm); fd.append('a', a); fd.append('b', b);
  await fetch('/overlaps/ignore-except', { method: 'POST', body: fd });
}
async function muteOtherOverlaps(a, b) {
  // keep the a–b pair, mute every other overlap involving EITHER a or b, then reload
  try { await _ignoreExcept(a, a, b); await _ignoreExcept(b, a, b); } catch (e) {}
  _reloadOverlaps();
}
async function neverShowOverlaps(ytm) {
  // ignore a playlist's overlaps entirely (including this pair), then reload
  const fd = new FormData(); fd.append('ytm', ytm);
  try { await fetch('/overlaps/ignore', { method: 'POST', body: fd }); } catch (e) {}
  _reloadOverlaps();
}
// distinct colors for member badges A,B,C…N (index → palette)
function memberColor(i) {
  const palette = ['#7c6cff', '#4fd6e0', '#ff6b8b', '#6bffab', '#f4c66a', '#c08cff', '#ff9d5c', '#5cc8ff'];
  return palette[i % palette.length];
}
function mergeEditor(members, tracks, returnTo) {
  return {
    members, tracks, returnTo: returnTo || '/cleanup',
    keep: String(members[0].id), busy: false, err: '', inc: {}, pick: {},
    // Two independent axes:
    //   sort: 'alpha' (by title) | 'playlist' (order-preserving merge by position)
    //   mode: 'interleaved' (all together) | 'ducks' (shared first, odd ducks pushed to the end)
    sort: 'playlist', mode: 'interleaved',
    init() { this.tracks.forEach(t => { this.inc[t.tid] = true; }); },   // default: include everything
    presentCount(t) { return t.present.filter(Boolean).length; },
    // Effective normalized position: a per-track chosen playlist's position, else the average.
    effPos(t) {
      const p = this.pick[t.tid];
      if (p != null && t.npos && t.npos[p] != null) return t.npos[p];
      const vals = (t.npos || []).filter(v => v != null);
      return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 1;
    },
    pickPos(t, i) {
      if (!t.present[i] || this.presentCount(t) < 2) return;   // nothing to choose
      this.pick[t.tid] = (this.pick[t.tid] === i) ? null : i;  // toggle
    },
    isPicked(t, i) { return this.pick[t.tid] === i; },
    isIn(t) { return !!this.inc[t.tid]; },
    toggle(t) { this.inc[t.tid] = !this.inc[t.tid]; },
    setAll(v) { this.tracks.forEach(t => { this.inc[t.tid] = v; }); },
    count() { return this.tracks.filter(t => this.isIn(t)).length; },
    letters(t) { return this.members.filter((m, i) => t.present[i]).map(m => m.letter); },
    colorOf(letter) { return memberColor(letter.charCodeAt(0) - 65); },
    posOf(t, letter) { return (t.pos && t.pos[letter.charCodeAt(0) - 65]) || ''; },   // 1-based index in that playlist
    fmtDur(s) {
      if (s == null || isNaN(s) || s <= 0) return '';
      s = Math.round(s);
      const m = Math.floor(s / 60), sec = s % 60;
      return m + ':' + String(sec).padStart(2, '0');
    },
    ordered() {
      const byTitle = (a, b) => (a.title || '').localeCompare(b.title || '');
      const byPos = (a, b) => this.effPos(a) - this.effPos(b) || byTitle(a, b);
      const within = this.sort === 'playlist' ? byPos : byTitle;   // sort axis
      const cnt = t => t.present.filter(Boolean).length;
      if (this.mode === 'ducks') {                                   // shared first, odd ducks last
        const grp = t => (cnt(t) >= 2 ? 0 : 1);
        return [...this.tracks].sort((a, b) => (grp(a) - grp(b)) || within(a, b));
      }
      return [...this.tracks].sort(within);                         // interleaved: one combined list
    },
    async apply() {
      if (this.busy) return; this.busy = true; this.err = '';
      const vids = this.ordered().filter(t => this.isIn(t)).map(t => t.video_id).filter(Boolean);
      const ids = this.members.map(m => m.id).join(',');
      const fd = new FormData();
      fd.append('ids', ids); fd.append('result', vids.join(',')); fd.append('keep', this.keep);
      try {
        const r = await fetch('/merge/apply', { method: 'POST', body: fd });
        const j = await r.json();
        if (j.ok) {
          const sep = this.returnTo.includes('?') ? '&' : '?';
          let u = this.returnTo + sep + 'flash=' + encodeURIComponent(j.message);
          if (j.playlist) u += '&flash_pl=' + encodeURIComponent(j.playlist);
          location.href = u;
        } else { this.busy = false; this.err = j.error || 'failed'; }
      } catch (e) { this.busy = false; this.err = String(e); }
    },
  };
}
function titleEditor(pid) {
  // click the playlist <h1> to rename it (YouTube + store). The title stays server-rendered in the
  // <h1> (Jinja escapes it safely); we read/write its text rather than threading it through x-data.
  return {
    pid: pid, editing: false, draft: '',
    start() {
      this.draft = this.$refs.h1.textContent.trim();
      this.editing = true;
      this.$nextTick(() => { this.$refs.inp.focus(); this.$refs.inp.select(); });
    },
    cancel() { this.editing = false; },
    async save() {
      if (!this.editing) return;                 // ignore trailing blur after enter/escape
      this.editing = false;
      const t = (this.draft || '').trim();
      const cur = this.$refs.h1.textContent.trim();
      if (!t || t === cur) return;
      try {
        const r = await fetch(`/playlist/${this.pid}/rename`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: t }),
        });
        const j = await r.json();
        if (j.ok) { this.$refs.h1.textContent = t; document.title = t + ' · yt-playlist'; }
      } catch (e) { /* leave the old title in place */ }
    },
  };
}
function enrichPanel(pid, lastfmConfigured, activeJobId, activeSource) {
  // MusicBrainz enrichment: background job streamed over SSE. Updates the Year/Genre cells live
  // as each track resolves, and drives a determinate progress bar.
  return {
    pid: pid, lastfmConfigured: lastfmConfigured, running: false, finished: false, pct: 0, status: '', source: '',
    // Last.fm API key modal
    keyModal: false, keyValue: '', keyBusy: false, keyErr: '',
    lastfmClick() {
      if (this.lastfmConfigured) this.start('lastfm');
      else { this.keyErr = ''; this.keyModal = true; }
    },
    async saveKey() {
      const key = (this.keyValue || '').trim();
      if (!key) { this.keyErr = 'Enter a key.'; return; }
      this.keyBusy = true; this.keyErr = '';
      try {
        const r = await fetch('/settings/lastfm-key', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }),
        });
        const j = await r.json();
        this.keyBusy = false;
        if (!j.configured) { this.keyErr = 'Could not save that key.'; return; }
        this.lastfmConfigured = true; this.keyModal = false; this.keyValue = '';
        this.start('lastfm');                       // saved — run it now
      } catch (e) { this.keyBusy = false; this.keyErr = 'Network error.'; }
    },
    // if the page was refreshed while a job is running, reattach to it and resume the progress UI
    rejoinIfActive() {
      if (activeJobId) {
        this.status = 'Reconnecting to enrichment in progress…';
        this.listen(activeJobId, activeSource);
      }
    },
    async start(source) {
      if (this.running) return;
      this.source = source;
      this.running = true; this.finished = false; this.pct = 0;
      this.status = source === 'lastfm' ? 'Starting Last.fm tagging…' : 'Starting enrichment…';
      let job;
      try {
        const r = await fetch(`/playlist/${this.pid}/enrich/${source}`, { method: 'POST' });
        job = (await r.json()).job_id;
      } catch (e) { this.status = 'Could not start.'; this.running = false; return; }
      this.listen(job, source);
    },
    listen(job, source) {
      this.source = source; this.running = true; this.finished = false;
      const es = new EventSource(`/playlist/enrich/events/${job}`);
      es.onmessage = (m) => {
        const ev = JSON.parse(m.data);
        if (ev.type === 'track') {
          this.applyRow(ev);
          this.pct = Math.round((ev.i / ev.n) * 100);
          this.status = ev.text;
        } else if (ev.type === 'info') {
          this.status = ev.text;
        } else if (ev.type === 'done') {
          this.pct = 100; this.status = ev.text;
        } else if (ev.type === 'err') {
          this.status = ev.text;
          if (ev.text && ev.text.includes('Last.fm API key')) {   // key missing/invalid — prompt for it
            this.lastfmConfigured = false; this.keyModal = true;
          }
        } else if (ev.type === 'end') {
          es.close(); this.running = false; this.finished = true;
          if (!ev.error) setTimeout(() => { this.finished = false; }, 4000);
        }
      };
      es.onerror = () => { es.close(); this.running = false; this.status = 'Stream interrupted.'; };
    },
    // patch a row's Year/Genre cells live — but only the fields this event carries (MusicBrainz
    // sends both, Last.fm sends only genre, so we must not blank year on a Last.fm run)
    applyRow(ev) {
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(ev.video_id)}"]`);
      if (!tr) return;
      if ('year' in ev) {
        const y = tr.querySelector('.ydisplay');
        if (y) y.innerHTML = ev.year ? escapeHtml(ev.year) : '<span class="muted ghint">＋</span>';
        tr.dataset.year = ev.year || 0;
      }
      if ('genre' in ev) {
        const g = tr.querySelector('.gdisplay');
        if (g) g.innerHTML = genreChip(ev.genre);
        tr.dataset.genre = (ev.genre || '').toLowerCase();
      }
    },
  };
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function genreChip(genre) {
  return genre ? `<span class="gtag">${escapeHtml(genre)}</span>` : '<span class="muted ghint">＋</span>';
}
function authBanner(initial) {
  // The "session expired" bar — seeded from the server, and updated live by the sync panel so it
  // pops up during an AJAX sync (no page reload needed).
  return {
    labels: initial || [],
    add(label) { if (label && !this.labels.includes(label)) this.labels.push(label); },
  };
}
function syncPanel() {
  return {
    running: false,
    failed: false,
    lines: [],
    push(ev) {
      const bad = ev.type === 'err' || ev.type === 'auth_expired';
      const pip = bad ? '✗' : ev.type === 'done' || ev.type === 'end' ? '✓'
                : ev.type === 'step' ? '›' : '•';
      const cls = bad ? 'err' : (ev.type === 'done' || ev.type === 'end') ? 'done' : '';
      this.lines.push({ text: ev.text || '', pip, cls });
      this.$nextTick(() => { const l = this.$refs.log; if (l) l.scrollTop = l.scrollHeight; });
    },
    async start() {
      if (this.running) return;
      this.running = true; this.failed = false; this.lines = [];
      this.push({ type: 'info', text: 'starting sync…' });
      let job;
      try {
        const r = await fetch('/sync', { method: 'POST' });
        job = (await r.json()).job_id;
      } catch (e) { this.push({ type: 'err', text: String(e) }); this.running = false; this.failed = true; return; }
      const es = new EventSource(`/sync/events/${job}`);
      es.onmessage = (m) => {
        const ev = JSON.parse(m.data);
        if (ev.type === 'err' || ev.type === 'auth_expired') this.failed = true;   // keep the log open
        if (ev.type === 'auth_expired' && ev.label)        // pop the "session expired" banner live
          window.dispatchEvent(new CustomEvent('auth-expired', { detail: { label: ev.label } }));
        if (ev.type === 'end') {
          es.close(); this.running = false;
          if (!ev.error && !this.failed) {
            this.push({ type: 'done', text: 'reloading…' });
            setTimeout(() => location.reload(), 700);
          } else {
            this.push({ type: 'err', text: 'finished with errors — log kept open. Reload when ready.' });
          }
          return;
        }
        this.push(ev);
      };
      es.onerror = () => { es.close(); this.running = false; this.failed = true; this.push({ type: 'err', text: 'stream interrupted' }); };
    },
  };
}
