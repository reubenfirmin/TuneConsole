// Keep --topbar-h in sync with the real navbar height so sticky table headers pin right below it.
function syncTopbarH() {
  const tb = document.querySelector('.topbar');
  if (tb) document.documentElement.style.setProperty('--topbar-h', tb.offsetHeight + 'px');
}
window.addEventListener('DOMContentLoaded', syncTopbarH);
window.addEventListener('resize', syncTopbarH);

// htmx: a 422 carries an OOB error toast. By default htmx won't process a 4xx body,
// so opt this status in. The server sets `HX-Reswap: none` to keep the primary
// target untouched while the OOB toast still lands in #toasts. Bind on `document`
// (not document.body): app.js loads in <head>, before <body> exists.
document.addEventListener('htmx:beforeSwap', (e) => {
  if (e.detail.xhr.status === 422) { e.detail.shouldSwap = true; e.detail.isError = false; }
});

// Alpine component factories for the various pages (loaded globally via base.html).
function rowSort(pid, editBase) {
  // Generic click-to-sort for a static-row table; reorders <tr class="srow"> by data-<key>.
  // Numeric when both values parse as numbers, else locale string compare.
  // Also hosts the per-row "⋯" menu and the "find alternate versions" flow for the playlist view.
  // `editBase` is the URL the genre/year edits POST under (defaults to /playlist/<pid>; the album
  // page passes /album/<browse>). Reorder/remove stay playlist-only.
  return {
    pid: pid, editBase: editBase || ('/playlist/' + pid), key: '', dir: 1,
    openMenu: null,                                   // video_id whose ⋯ menu is open
    // alternate-versions modal
    altOpen: false, altLoading: false, altTitle: '',
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
      // A manual drag-reorder only makes sense in the playlist's TRUE order. Once a column sort is
      // applied, the on-screen order isn't canonical, so persisting a single drag would scramble the
      // real order. Disable dragging until the view is reloaded back to the default order.
      if (this._sortable) this._sortable.option('disabled', this.key !== '');
    },
    ind(k) { return this.key === k ? (this.dir === 1 ? ' ▲' : ' ▼') : ''; },

    // Open the modal and let htmx fetch + render the results (server builds the list; we only own the
    // modal open/close + loading flag). The "Add" button hx-includes the checked results.
    findAlternates(vid, title) {
      this.openMenu = null;
      this.altOpen = true; this.altLoading = true; this.altTitle = title;
      htmx.ajax('GET', `/playlist/${this.pid}/alternates?video_id=${encodeURIComponent(vid)}`,
        { target: '#alt-results', swap: 'innerHTML' }).finally(() => { this.altLoading = false; });
    },

    // "Songs like this": server renders the modal (with selectable rows + an Add button) into
    // #similar-modal. Pass the playlist id so the modal can offer "add below this track"; the seed
    // vid becomes the insert anchor on the server side.
    songsLike(vid) {
      this.openMenu = null;
      htmx.ajax('GET', `/track/${encodeURIComponent(vid)}/similar?pid=${this.pid}`,
        { target: '#similar-modal', swap: 'innerHTML' });
    },

    // remove-track confirmation modal
    rmOpen: false, rmBusy: false, rmErr: '', rmVid: '', rmTitle: '',
    removeTrack(vid, title) {
      this.openMenu = null;
      this.rmVid = vid; this.rmTitle = title; this.rmErr = ''; this.rmOpen = true;
    },
    async confirmRemove() {
      this.rmBusy = true;
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(this.rmVid)}"]`);
      // htmx owns request + swap: success returns an empty row -> the <tr> is removed; an error
      // returns the OOB toast and leaves the row in place.
      try {
        await htmx.ajax('POST', `/playlist/${this.pid}/remove-track`,
          { values: { video_id: this.rmVid }, target: tr, swap: 'outerHTML' });
      } catch (e) { /* leave the row; a reload would resync */ }
      this.rmBusy = false; this.rmOpen = false;
    },

    // --- drag-and-drop reorder via SortableJS (floating drag clone + ghost placeholder) ---
    initSortable() {
      const tb = this.$refs.body;
      if (!tb || typeof Sortable === 'undefined') return;
      this._sortable = Sortable.create(tb, {
        handle: '.drag-handle',
        disabled: this.key !== '',           // only draggable in the true (unsorted) order
        draggable: 'tr.srow',
        animation: 160,
        // Native drag image (a faithful, full-width snapshot of the row) floats under the cursor.
        // A fallback clone would detach the <tr> from the table and collapse its columns.
        ghostClass: 'srow-ghost',            // the placeholder shown at the insert point
        chosenClass: 'srow-chosen',
        onEnd: (e) => {
          this.renumber();
          if (e.oldIndex === e.newIndex) return;
          const rows = tb.querySelectorAll('tr.srow');
          const moved = e.item.dataset.vid;
          const next = e.item.nextElementSibling;
          const beforeVid = next && next.classList.contains('srow') ? next.dataset.vid : '';
          // htmx persists the new order (no swap: the DOM is already reordered); on failure the
          // server replies HX-Refresh to reload and resync the true order.
          htmx.ajax('POST', `/playlist/${this.pid}/reorder`,
            { values: { video_id: moved, before_video_id: beforeVid }, swap: 'none' });
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
      if (!tr) return;
      // htmx owns the request + swap: the server re-renders the whole row, keeping the data-*
      // the sort reads in sync. (Alpine just triggers it; it never builds the HTML itself.)
      try {
        await htmx.ajax('POST', `${this.editBase}/track-genre`,
          { values: { video_id: vid, genre }, target: tr, swap: 'outerHTML' });
      } catch (e) { /* leave the row as-is; a reload would resync */ }
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
      if (!tr) return;
      try {
        await htmx.ajax('POST', `${this.editBase}/track-year`,
          { values: { video_id: vid, year }, target: tr, swap: 'outerHTML' });
      } catch (e) { /* leave the row as-is; a reload would resync */ }
    },

    // --- click-to-edit title / artist (free text, edit in place) ---
    editTitleVid: null, editArtistVid: null,
    _startEdit(vid, which, selector, dispSel) {
      this[which] = vid;
      this.$nextTick(() => {
        const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(vid)}"]`);
        const inp = tr && tr.querySelector(selector);
        const disp = tr && tr.querySelector(dispSel);
        if (inp) {
          // seed from the link text, stripping the trailing " ↗" the title link carries
          inp.value = (disp ? disp.textContent : '').replace(/↗/g, '').trim();
          inp.focus(); inp.select();
        }
      });
    },
    startEditTitle(vid) { this._startEdit(vid, 'editTitleVid', '.tinput', '.ptitle'); },
    startEditArtist(vid) { this._startEdit(vid, 'editArtistVid', '.ainput', '.alink'); },
    async _saveField(vid, value, which, path, field) {
      if (this[which] !== vid) return;        // ignore trailing blur after enter/escape
      this[which] = null;
      const v = (value || '').trim();
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(vid)}"]`);
      if (!tr) return;
      try {
        await htmx.ajax('POST', `${this.editBase}/${path}`,
          { values: { video_id: vid, [field]: v }, target: tr, swap: 'outerHTML' });
      } catch (e) { /* leave the row as-is; a reload would resync */ }
    },
    saveTitle(vid, value) { return this._saveField(vid, value, 'editTitleVid', 'track-title', 'title'); },
    saveArtist(vid, value) { return this._saveField(vid, value, 'editArtistVid', 'track-artist', 'artist'); },
    async resetField(vid, field) {
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(vid)}"]`);
      if (!tr) return;
      try {
        await htmx.ajax('POST', `${this.editBase}/track-reset`,
          { values: { video_id: vid, field }, target: tr, swap: 'outerHTML' });
      } catch (e) { /* leave the row as-is */ }
    },
  };
}
function overlapSort() {
  // Client-side sort of the overlaps table by reordering the per-row <tbody> nodes
  // (which preserves each row's Alpine state, pie menu, hide animation, etc.).
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
    // the "hide this + all below" pairs, for the confirm modal's htmx post (see cleanup.html)
    pairsJson() { return JSON.stringify(this._below.map(r => [r.dataset.ay, r.dataset.by])); },
    ind(k) { return this.key === k ? (this.dir === 1 ? ' ▲' : ' ▼') : ''; },
  };
}
function playlistsTab(rows) {
  return {
    rows, sel: {}, sortKey: 'title', sortDir: 1, split: false, busy: false,
    groupModal: false, groupName: '', delModal: false, collapsed: {},
    init() {
      // remember view preferences across reloads (the tab reloads after group/delete)
      try {
        this.split = localStorage.getItem('pl.split') === '1';
        this.sortKey = localStorage.getItem('pl.sortKey') || 'title';
        this.sortDir = +localStorage.getItem('pl.sortDir') || 1;
        this.collapsed = JSON.parse(localStorage.getItem('pl.collapsed') || '{}');
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
    toggleGen() {
      this.collapsed.Generated = !this.collapsed.Generated;
      try { localStorage.setItem('pl.collapsed', JSON.stringify(this.collapsed)); } catch (e) {}
    },
    // "Generated" is pinned into its own card above the table (see template), never in the sections.
    // Always newest-first by creation time (independent of the main table's column sort).
    genRows() {
      return this.rows.filter(r => r.group === 'Generated')
        .sort((a, b) => (b.created || 0) - (a.created || 0));
    },
    promote(r) {
      // graduate a Generated playlist into the library (out of the quarantine group), then reload
      fetch('/playlist/' + r.id + '/promote', { method: 'POST', headers: { 'HX-Request': 'true' } })
        .then(() => location.reload());
    },
    sections() {
      // the main table holds everything EXCEPT Generated; split partitions by group (Ungrouped last)
      const rest = this.sorted().filter(r => r.group !== 'Generated');
      if (!this.split) return [{ name: '', rows: rest }];
      const m = {};
      rest.forEach(r => { const g = r.group || 'Ungrouped'; (m[g] = m[g] || []).push(r); });
      return Object.keys(m)
        .sort((a, b) => a === 'Ungrouped' ? 1 : b === 'Ungrouped' ? -1 : a.localeCompare(b))
        .map(n => ({ name: n, rows: m[n] }));
    },
    selectAll(on) { this.rows.forEach(r => { this.sel[r.id] = on; }); },
    fmtLast(ts) {
      if (!ts) return '-';
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
    copyModal: false, copyName: '', copyIds: [],
    openCopy() {
      const sel = this.selected();
      if (!sel.length) return;
      this.copyIds = sel.map(r => r.id);
      // single -> "Title (copy)"; multiple -> combine-to-new, prefilled with the joined names
      this.copyName = sel.length === 1 ? sel[0].title + ' (copy)' : sel.map(r => r.title).join(' + ');
      this.copyModal = true;
    },
    copyIntoModal: false, copyIntoTarget: '',
    openCopyInto() {
      const sel = this.selected();
      if (!sel.length) return;
      this.copyIds = sel.map(r => r.id);
      this.copyIntoTarget = '';
      this.copyIntoModal = true;
    },
    // destinations for "Copy into…": every playlist except the selected sources, by title
    intoTargets() {
      const src = new Set(this.copyIds);
      return [...this.rows].filter(r => !src.has(r.id))
        .sort((a, b) => a.title.localeCompare(b.title));
    },
    openGroup() { if (this.count()) { this.groupName = ''; this.groupModal = true; } },
    // Modal "save" actions go through htmx.ajax so the server's HX-Refresh reload (success) and
    // 422 OOB error toast (copy-into) behave exactly as the inline hx-post did, but the values
    // come straight from this component's state, not a global Alpine.$data() reach-in.
    copy() { htmx.ajax('POST', '/playlists/copy', { values: { ids: this.copyIds.join(','), name: this.copyName } }); },
    copyInto() { htmx.ajax('POST', '/playlists/copy-into', { values: { ids: this.copyIds.join(','), target: this.copyIntoTarget } }); },
    group() { htmx.ajax('POST', '/playlists/group', { values: { ids: this.selected().map(r => r.id).join(','), name: this.groupName } }); },
    remove() { htmx.ajax('POST', '/playlists/delete', { values: { ids: this.selected().map(r => r.id).join(',') } }); },
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
  };
}
function enrichPanel(pid, lastfmConfigured, activeJobId, activeSource, enrichBase, conflictCount) {
  // Enrichment: one background job runs the configured provider waterfall and streams over SSE.
  // Updates the Year/Genre cells live as each track resolves, drives a determinate progress bar, and
  // surfaces a conflict count (provider disagreements) that lights up the header's resolve icon.
  // `enrichBase` defaults to /playlist/<pid>; album pages pass /album/<browse>.
  return {
    pid: pid, enrichBase: enrichBase || ('/playlist/' + pid),
    lastfmConfigured: lastfmConfigured, running: false, finished: false, pct: 0, status: '', source: '',
    conflictCount: conflictCount || 0,
    openConflicts() {
      htmx.ajax('GET', `${this.enrichBase}/conflicts`, { target: '#conflicts-modal', swap: 'innerHTML' });
    },
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
        await htmx.ajax('POST', '/settings/lastfm-key', { values: { key }, swap: 'none' });
        this.keyBusy = false;
        this.lastfmConfigured = true; this.keyModal = false; this.keyValue = '';
        this.start('lastfm');                       // saved, run it now
      } catch (e) { this.keyBusy = false; this.keyErr = 'Could not save that key.'; }
    },
    // if the page was refreshed while a job is running, reattach to it and resume the progress UI
    rejoinIfActive() {
      if (activeJobId) {
        this.status = 'Reconnecting to enrichment in progress…';
        this.listen(activeJobId, activeSource);
      }
    },
    async start() {
      if (this.running) return;
      this.source = 'waterfall';
      this.running = true; this.finished = false; this.pct = 0;
      this.status = 'Starting enrichment…';
      let job;
      try {
        const r = await fetch(`${this.enrichBase}/enrich`, { method: 'POST' });
        job = (await r.json()).job_id;
      } catch (e) { this.status = 'Could not start.'; this.running = false; return; }
      this.listen(job, 'waterfall');
    },
    listen(job, source) {
      this.source = source; this.running = true; this.finished = false;
      const es = new EventSource(`/playlist/enrich/events/${job}`);
      let errs = 0;
      es.onmessage = (m) => {
        errs = 0;                          // a delivered event means we're reconnected: reset backoff
        const ev = JSON.parse(m.data);
        if (ev.type === 'track') {
          this.applyRow(ev);
          this.pct = Math.round((ev.i / ev.n) * 100);
          this.status = ev.text;
        } else if (ev.type === 'info') {
          this.status = ev.text;
        } else if (ev.type === 'done') {
          this.pct = 100; this.status = ev.text;
          if (typeof ev.conflicts === 'number') this.conflictCount = ev.conflicts;
        } else if (ev.type === 'err') {
          this.status = ev.text;
          if (ev.text && ev.text.includes('Last.fm API key')) {   // key missing/invalid: prompt for it
            this.lastfmConfigured = false; this.keyModal = true;
          }
        } else if (ev.type === 'end') {
          es.close(); this.running = false; this.finished = true;
          if (!ev.error) setTimeout(() => { this.finished = false; }, 4000);
        }
      };
      es.onerror = () => {
        // A transient drop (e.g. a proxy idle-timeout): let EventSource auto-reconnect. The server
        // replays events idempotently and sends 'end' once the job finishes, so a successful
        // background job no longer looks failed. Give up only after several consecutive failures.
        this.status = 'Reconnecting…';
        if (++errs >= 5) { es.close(); this.running = false; this.status = 'Stream interrupted. Reload to check.'; }
      };
    },
    // The SSE event carries the server-rendered row HTML (same partial as a manual edit), so we just
    // drop it in. Alpine re-inits the replaced <tr>, and its data-* (which sort reads) come along.
    applyRow(ev) {
      if (!ev.row_html) return;
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(ev.video_id)}"]`);
      if (tr) tr.outerHTML = ev.row_html;
    },
  };
}
function authBanner(initial) {
  // The "session expired" bar, seeded from the server, and updated live by the sync panel so it
  // pops up during an AJAX sync (no page reload needed).
  return {
    labels: initial || [],
    add(label) { if (label && !this.labels.includes(label)) this.labels.push(label); },
  };
}
function homeStatus() {
  // Home status card: polls GET /bridge/status every ~3s for the live extension connection and the
  // now-playing track. Library syncing is automatic in the background, so the only action here is an
  // unobtrusive "Refresh library" link that kicks a manual POST /sync for power users.
  return {
    connected: false,
    nowPlaying: null,
    refreshing: false,
    init() {
      const poll = () => fetch('/bridge/status').then(r => r.json())
        .then(d => { this.connected = !!d.connected; this.nowPlaying = d.now_playing || null; })
        .catch(() => {});
      poll();
      setInterval(poll, 3000);
    },
    async refresh() {
      if (this.refreshing) return;
      this.refreshing = true;
      // Kicking off a sync retires the setup flash (same event the old sync bar dispatched).
      window.dispatchEvent(new CustomEvent('sync-started'));
      if (location.search.includes('flash=')) history.replaceState({}, '', location.pathname);
      try { await fetch('/sync', { method: 'POST' }); } catch (e) {}
      // The library sync runs in the background (rec/enrich workers pick it up); just hold the link
      // in its "Refreshing…" state briefly so the click reads as acknowledged.
      setTimeout(() => { this.refreshing = false; }, 4000);
    },
    rate(action) {
      if (!this.nowPlaying) return;
      const want = action === 'like' ? 'LIKE' : 'DISLIKE';
      // Optimistic toggle (clicking the active one clears it, matching YouTube Music); the next poll
      // reconciles with the real state the extension read back from the player.
      this.nowPlaying.likeStatus = this.nowPlaying.likeStatus === want ? 'INDIFFERENT' : want;
      fetch('/now-playing/rate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      }).catch(() => {});
    },
  };
}

// Route every YouTube Music play/open link through the extension so it plays in the EXISTING YouTube
// Music tab (in the background, you stay on TuneConsole) instead of opening a new tab. Falls back to
// opening the link when the extension is not connected. Ctrl/meta/middle-click still open a new tab.
function tcPlay(url) {
  return fetch('/play', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }),
  }).then(r => r.json()).then(d => {
    if (!d || !d.ok) window.open(url, '_blank', 'noopener');   // extension off: open it the old way
  }).catch(() => { window.open(url, '_blank', 'noopener'); });
}
window.tcPlay = tcPlay;
// Capture phase (the `true` below) so we run BEFORE the target's own handlers: some play links live
// inside modals and carry @click.stop, which would hide the click from a normal bubble listener.
document.addEventListener('click', function (e) {
  if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  var a = e.target.closest && e.target.closest('a[href*="music.youtube.com/"]');
  if (!a) return;
  var u;
  try { u = new URL(a.href || a.getAttribute('href'), location.href); } catch (err) { return; }
  if (u.hostname !== 'music.youtube.com') return;
  // Any content path (watch / playlist / browse / channel / ...) routes to the existing tab. Only the
  // bare origin (the "sign in" link) is left to open normally so you can actually see the sign-in page.
  if (u.pathname === '/' || u.pathname === '') return;
  e.preventDefault();
  tcPlay(u.href);
}, true);

// Navbar omnisearch dropdown: open/close + keyboard nav over the HTMX-rendered result rows.
// Visibility is driven by results arriving (htmx:afterSwap, wired in x-init on the form) and by
// focus; we never build markup here. The server owns the dropdown body.
function omniSearch() {
  return {
    open: false,
    active: -1,
    rows() { return Array.from(this.$root.querySelectorAll('.omni-row')); },
    onResults() {
      this.active = -1;
      this.rows().forEach(r => r.classList.remove('active'));
      this.open = this.rows().length > 0;
    },
    close() {
      this.open = false;
      this.active = -1;
      this.rows().forEach(r => r.classList.remove('active'));
    },
    move(dir) {
      const rows = this.rows();
      if (!rows.length) { return; }
      this.open = true;
      this.active = (this.active + dir + rows.length) % rows.length;
      rows.forEach((r, i) => r.classList.toggle('active', i === this.active));
      rows[this.active].scrollIntoView({ block: 'nearest' });
    },
    choose() {
      const rows = this.rows();
      const row = rows[this.active] || rows[0];
      if (row) { row.click(); this.close(); }
    },
  };
}

// A genre row's subgenre drill-down toggle. The open/closed state is kept per-family on `window`
// (not localStorage: it's session-scoped, not worth persisting across reloads) so it survives the
// #home-feed re-render that fires when you steer a bar. Without this, adjusting a subgenre would
// re-render the panel and collapse the drill-down you were working in. Family name comes from
// data-fam (safe for any name; no string interpolation into the expression).
function fpGenre() {
  return {
    open: false,
    fam: '',
    // Capture the family name at init, where $el is the component root (in a method fired from the
    // button's @click, $el would be the button instead, which has no data-fam).
    init() {
      this.fam = this.$el.dataset.fam || '';
      this.open = !!(window.__fpOpen && window.__fpOpen[this.fam]);
    },
    toggle() {
      this.open = !this.open;
      (window.__fpOpen = window.__fpOpen || {})[this.fam] = this.open;
    },
  };
}

// Live center-anchored fill for the Home nudge sliders (#2): update --p (thumb position as a %) on
// drag so the track fills from neutral out to the thumb in real time. Delegated on document so it
// covers sliders re-rendered by htmx swaps; the initial value is set inline per-render server-side.
document.addEventListener('input', function (e) {
  var s = e.target;
  if (s && s.classList && (s.classList.contains('fp-slider') || s.classList.contains('fp-breadth-slider'))) {
    var pct = (s.value - s.min) / (s.max - s.min) * 100;
    s.style.setProperty('--p', pct + '%');
  }
});

// The Home "Your taste" panel's collapse toggle. State persists in localStorage so it survives full
// reloads AND every #home-feed htmx swap (each swap re-creates this component, which re-reads the
// flag on init). Plain localStorage (no Alpine persist plugin needed).
function fpPanel() {
  return {
    collapsed: localStorage.getItem('fp_collapsed') === '1',
    toggle() {
      this.collapsed = !this.collapsed;
      localStorage.setItem('fp_collapsed', this.collapsed ? '1' : '0');
    },
  };
}

// Home taste-bar genre picker: an autosuggest combo (same widget family as the Clusters genre
// filter) for pinning a steerable genre bar. The full taxonomy is fetched once and cached on
// `window`, then filtered CLIENT-SIDE so typing, reset, and close are instant (the old server-htmx
// search left stale "Add" rows behind on clear).
//
// A pick POSTs to /home/fingerprint/add and swaps ONLY #fp-genre-bars with the server's re-rendered
// bars (the new one included), then re-processes that subtree for htmx. Deliberately a plain fetch +
// innerHTML (not htmx.ajax targeting #home-feed): swapping the whole feed would destroy and recreate
// THIS component on every add, and that destroy/re-init churn is what made adds land only sometimes.
// Keeping the picker outside the swapped region makes every add reliable.
function genrePicker() {
  return {
    opts: [], query: '', open: false, sel: -1,
    load() {
      if (window.__genreOpts) { this.opts = window.__genreOpts; return; }
      fetch('/home/genres').then(r => r.json())
        .then(d => { window.__genreOpts = d.options || []; this.opts = window.__genreOpts; })
        .catch(() => {});
    },
    suggest() {
      const q = this.query.trim().toLowerCase();
      if (!q) return [];                                    // empty field -> no dropdown (reset state)
      // Tokenized, order-independent match: every word you type must appear somewhere in the name, so
      // "rock post", "rock-post" and "post rock" all find "post-rock" (a plain substring wouldn't).
      const toks = q.split(/[^a-z0-9]+/).filter(Boolean);
      return this.opts.filter(o => { const n = o.name.toLowerCase(); return toks.every(t => n.includes(t)); })
        .slice(0, 10);
    },
    move(d) { const n = this.suggest().length; if (n) this.sel = (this.sel + d + n) % n; },
    choose() { const o = this.suggest()[this.sel >= 0 ? this.sel : 0]; if (o) this.pick(o.name); },
    pick(name) {
      this.reset();
      fetch('/home/fingerprint/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'axis=genre:' + encodeURIComponent(name),
      }).then(r => r.text()).then(html => {
        const bars = document.getElementById('fp-genre-bars');
        if (!bars) return;
        bars.innerHTML = html;                       // server-rendered bars (dedup + order correct)
        if (window.htmx) htmx.process(bars);         // wire the bars' hx-* sliders / expanders
      }).catch(() => {});
    },
    reset() { this.query = ''; this.open = false; this.sel = -1; },
  };
}

// Setup → Enrichment tab: drag to reorder metadata providers (SortableJS, same as playlist rows),
// toggle to enable/disable. `providers` carries each {name, requires}; a provider must stay after
// the one it requires (AcousticBrainz after MusicBrainz), enforced live in onMove so the DOM never
// reaches an invalid state. Order + enabled flags persist to /setup/enrichment (config only).
function enrichmentTab(providers) {
  const reqs = providers.filter(p => p.requires).map(p => [p.name, p.requires]);
  return {
    initSortable() {
      const list = this.$refs.list;
      if (!list || typeof Sortable === 'undefined') return;
      Sortable.create(list, {
        handle: '.drag-handle',
        draggable: '.provider-card',
        animation: 160,
        ghostClass: 'srow-ghost',
        chosenClass: 'srow-chosen',
        onMove: (e) => this._allows(e),
        onEnd: () => this.persist(),
      });
    },
    // Would the proposed drop keep every provider after the one it requires? Simulate the move on a
    // names array and reject if any constraint inverts.
    _allows(e) {
      const names = [...this.$refs.list.querySelectorAll('.provider-card')].map(el => el.dataset.name);
      const dragged = e.dragged.dataset.name, related = e.related.dataset.name;
      names.splice(names.indexOf(dragged), 1);
      const rel = names.indexOf(related);
      names.splice(e.willInsertAfter ? rel + 1 : rel, 0, dragged);
      return reqs.every(([who, needs]) =>
        !names.includes(needs) || names.indexOf(who) > names.indexOf(needs));
    },
    persist() {
      const order = [...this.$refs.list.querySelectorAll('.provider-card')].map(el => el.dataset.name);
      const enabled = [...this.$refs.list.querySelectorAll('input[name="enabled"]:checked')]
        .map(el => el.value);
      htmx.ajax('POST', '/setup/enrichment', { values: { order, enabled }, swap: 'none' });
    },
  };
}

// Inline Last.fm API-key field inside the Enrichment tab's Last.fm card. Saves to the same
// /settings/lastfm-key route the per-playlist modal uses; flips to a "✓ saved" state on success.
function lastfmKey(configured) {
  return {
    saved: configured, editing: !configured, val: '', busy: false, err: '',
    save() {
      const key = (this.val || '').trim();
      if (!key) { this.err = 'Enter a key.'; return; }
      this.busy = true; this.err = '';
      htmx.ajax('POST', '/settings/lastfm-key', { values: { key }, swap: 'none' })
        .then(() => { this.busy = false; this.saved = true; this.editing = false; this.val = ''; })
        .catch(() => { this.busy = false; this.err = 'Could not save that key.'; });
    },
  };
}

// ── Taste-model visualization tooltips ──────────────────────────────────────
// A single floating div, formatted from each element's data-tip JSON (title + labelled rows).
// Document-level delegation so it also covers the lazily htmx-swapped embedding-engine panel.
function initVizTooltip() {
  let tip = null, current = null, hideTimer = null, plainText = '';
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  // A copy glyph so the user can grab the tooltip's text verbatim (to paste back as an example).
  const COPY_ICON = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<rect x="9" y="9" width="11" height="11" rx="2"/>'
    + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';

  function cancelHide() { clearTimeout(hideTimer); }
  function scheduleHide() { clearTimeout(hideTimer); hideTimer = setTimeout(hide, 240); }
  function hide() { if (tip) tip.classList.remove('on'); current = null; }

  function ensure() {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'viz-tip';
      tip.setAttribute('role', 'tooltip');
      // Interactive: the cursor can leave the source and move INTO the tip (to click copy)
      // without it vanishing.
      tip.addEventListener('mouseenter', cancelHide);
      tip.addEventListener('mouseleave', hide);
      tip.addEventListener('click', (e) => {
        const btn = e.target.closest('.vt-copy');
        if (!btn || !navigator.clipboard) return;
        navigator.clipboard.writeText(plainText).then(() => {
          btn.classList.add('done');
          setTimeout(() => btn.classList.remove('done'), 1200);
        }).catch(() => {});
      });
      document.body.appendChild(tip);
    }
    return tip;
  }
  function render(d) {
    const dot = /^#[0-9a-fA-F]{3,8}$/.test(d.dot || '') ? d.dot : '';
    const lines = [d.title];
    if (d.desc) lines.push(d.desc);
    let h = '<button class="vt-copy" type="button" title="Copy to clipboard" aria-label="Copy">'
          + COPY_ICON + '</button>';
    h += '<div class="vt-title' + (d.accent ? ' ' + d.accent : '') + '"'
       + (dot ? ' style="--dot:' + dot + '"' : '') + '>' + esc(d.title) + '</div>';
    if (d.desc) h += '<div class="vt-desc">' + esc(d.desc) + '</div>';
    for (const row of (d.rows || [])) {
      h += '<div class="vt-row' + (row.div ? ' vt-div' : '') + '">'
         + '<span class="vt-l">' + esc(row.l) + '</span>'
         + '<span class="vt-v' + (row.t ? ' ' + row.t : '') + '">' + esc(row.v) + '</span></div>';
      if (row.n) h += '<div class="vt-n">' + esc(row.n) + '</div>';
      lines.push(row.l + ': ' + row.v + (row.n ? ', ' + row.n : ''));
    }
    plainText = lines.join('\n');
    ensure().innerHTML = h;
  }
  function place(e) {
    if (!tip) return;
    const pad = 16, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = e.clientY - h - pad;
    tip.style.left = Math.max(8, x) + 'px';
    tip.style.top = Math.max(8, y) + 'px';
  }

  document.addEventListener('mouseover', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (!el) return;
    cancelHide();                       // re-entering a source (or hopping between them) keeps it up
    if (el === current) return;
    let d; try { d = JSON.parse(el.getAttribute('data-tip')); } catch (_) { return; }
    current = el; render(d); ensure().classList.add('on'); place(e);
  });
  // Pinned (no cursor-follow) so it's reachable; a short grace lets the pointer cross into the tip.
  document.addEventListener('mouseout', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (el && el === current && !el.contains(e.relatedTarget)) scheduleHide();
  });
  // A scroll or htmx swap can pull the hovered element out from under the cursor.
  document.addEventListener('scroll', hide, true);
  document.body.addEventListener('htmx:beforeSwap', hide);
}
window.addEventListener('DOMContentLoaded', initVizTooltip);

// hx-confirm -> styled modal (replaces the native window.confirm dialog). htmx fires `htmx:confirm`
// for any element carrying hx-confirm; we intercept it, show our own modal, and only issue the
// request if the user confirms. Reuses the .modal-backdrop/.modal/.modal-actions styles.
document.addEventListener('htmx:confirm', (e) => {
  const question = e.detail.question;
  if (!question) return;                       // no hx-confirm on this element: proceed normally
  e.preventDefault();                          // suppress htmx's native confirm

  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML =
    '<div class="modal" role="dialog" aria-modal="true">' +
    '<h3>Confirm</h3><p></p>' +
    '<div class="modal-actions">' +
    '<button type="button" class="btn-ghost" data-act="cancel">Cancel</button>' +
    '<button type="button" data-act="ok">Confirm</button>' +
    '</div></div>';
  backdrop.querySelector('p').textContent = question;   // textContent: never inject markup

  const close = () => backdrop.remove();
  const onKey = (ev) => { if (ev.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); } };
  backdrop.addEventListener('click', (ev) => { if (ev.target === backdrop) close(); });
  backdrop.querySelector('[data-act="cancel"]').addEventListener('click', close);
  backdrop.querySelector('[data-act="ok"]').addEventListener('click', () => {
    close();
    document.removeEventListener('keydown', onKey);
    e.detail.issueRequest(true);               // true = skip the confirm check, avoid recursion
  });
  document.addEventListener('keydown', onKey);
  document.body.appendChild(backdrop);
  backdrop.querySelector('[data-act="ok"]').focus();
});
