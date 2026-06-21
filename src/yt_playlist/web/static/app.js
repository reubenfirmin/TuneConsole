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
function rowSort(pid) {
  // Generic click-to-sort for a static-row table; reorders <tr class="srow"> by data-<key>.
  // Numeric when both values parse as numbers, else locale string compare.
  // Also hosts the per-row "⋯" menu and the "find alternate versions" flow for the playlist view.
  return {
    pid: pid, key: '', dir: 1,
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
          // htmx persists the new order (no swap — the DOM is already reordered); on failure the
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
        await htmx.ajax('POST', `/playlist/${this.pid}/track-genre`,
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
        await htmx.ajax('POST', `/playlist/${this.pid}/track-year`,
          { values: { video_id: vid, year }, target: tr, swap: 'outerHTML' });
      } catch (e) { /* leave the row as-is; a reload would resync */ }
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
    // the "hide this + all below" pairs, for the confirm modal's htmx post (see cleanup.html)
    pairsJson() { return JSON.stringify(this._below.map(r => [r.dataset.ay, r.dataset.by])); },
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
    copyModal: false, copyName: '', copyIds: [],
    openCopy() {
      const sel = this.selected();
      if (!sel.length) return;
      this.copyIds = sel.map(r => r.id);
      // single -> "Title (copy)"; multiple -> a copy+merge, prefilled with the joined names
      this.copyName = sel.length === 1 ? sel[0].title + ' (copy)' : sel.map(r => r.title).join(' + ');
      this.copyModal = true;
    },
    openGroup() { if (this.count()) { this.groupName = ''; this.groupModal = true; } },
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
        await htmx.ajax('POST', '/settings/lastfm-key', { values: { key }, swap: 'none' });
        this.keyBusy = false;
        this.lastfmConfigured = true; this.keyModal = false; this.keyValue = '';
        this.start('lastfm');                       // saved — run it now
      } catch (e) { this.keyBusy = false; this.keyErr = 'Could not save that key.'; }
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
    // The SSE event carries the server-rendered row HTML (same partial as a manual edit), so we just
    // drop it in — Alpine re-inits the replaced <tr>, and its data-* (which sort reads) come along.
    applyRow(ev) {
      if (!ev.row_html) return;
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(ev.video_id)}"]`);
      if (tr) tr.outerHTML = ev.row_html;
    },
  };
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
