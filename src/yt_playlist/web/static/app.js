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
      // real order — disable dragging until the view is reloaded back to the default order.
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

    // "Songs like this" — server renders the modal (with selectable rows + an Add button) into
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
    // "Generated" is pinned into its own card above the table (see template) — never in the sections.
    genRows() { return this.sorted().filter(r => r.group === 'Generated'); },
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
function enrichPanel(pid, lastfmConfigured, activeJobId, activeSource, enrichBase) {
  // Enrichment: background job streamed over SSE. Updates the Year/Genre cells live as each track
  // resolves, and drives a determinate progress bar. `enrichBase` is the URL the start POSTs under
  // (defaults to /playlist/<pid>; album pages pass /album/<browse>). The events stream is shared.
  return {
    pid: pid, enrichBase: enrichBase || ('/playlist/' + pid),
    lastfmConfigured: lastfmConfigured, running: false, finished: false, pct: 0, status: '', source: '',
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
        const r = await fetch(`${this.enrichBase}/enrich/${source}`, { method: 'POST' });
        job = (await r.json()).job_id;
      } catch (e) { this.status = 'Could not start.'; this.running = false; return; }
      this.listen(job, source);
    },
    listen(job, source) {
      this.source = source; this.running = true; this.finished = false;
      const es = new EventSource(`/playlist/enrich/events/${job}`);
      let errs = 0;
      es.onmessage = (m) => {
        errs = 0;                          // a delivered event means we're reconnected — reset backoff
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
      es.onerror = () => {
        // A transient drop (e.g. a proxy idle-timeout): let EventSource auto-reconnect — the server
        // replays events idempotently and sends 'end' once the job finishes — so a successful
        // background job no longer looks failed. Give up only after several consecutive failures.
        this.status = 'Reconnecting…';
        if (++errs >= 5) { es.close(); this.running = false; this.status = 'Stream interrupted. Reload to check.'; }
      };
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
function syncPanel(autoOn = false) {
  return {
    running: false,
    failed: false,
    auto: autoOn,
    lines: [],
    async toggleAuto() {
      const next = !this.auto;
      this.auto = next;   // optimistic: flip immediately so the note appears/disappears
      try {
        await fetch('/sync/auto', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: 'enabled=' + (next ? '1' : '0'),
        });
      } catch (e) { this.auto = !next; return; }   // revert if the server didn't take it
      if (next) this.start('/sync/plays');   // turning it on also runs an immediate live sync, as before
    },
    push(ev) {
      const bad = ev.type === 'err' || ev.type === 'auth_expired';
      const pip = bad ? '✗' : ev.type === 'done' || ev.type === 'end' ? '✓'
                : ev.type === 'step' ? '›' : '•';
      const cls = bad ? 'err' : (ev.type === 'done' || ev.type === 'end') ? 'done' : '';
      this.lines.push({ text: ev.text || '', pip, cls });
      this.$nextTick(() => { const l = this.$refs.log; if (l) l.scrollTop = l.scrollHeight; });
    },
    async start(endpoint = '/sync') {
      if (this.running) return;
      this.running = true; this.failed = false; this.lines = [];
      // Onboarding: kicking off a sync retires the setup flash and strips ?flash from the URL, so the
      // post-sync reload doesn't bring it back.
      window.dispatchEvent(new CustomEvent('sync-started'));
      if (location.search.includes('flash=')) history.replaceState({}, '', location.pathname);
      this.push({ type: 'info', text: 'starting sync…' });
      let job;
      try {
        const r = await fetch(endpoint, { method: 'POST' });
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
            this.push({ type: 'err', text: 'finished with errors. Log kept open. Reload when ready.' });
          }
          return;
        }
        this.push(ev);
      };
      es.onerror = () => { es.close(); this.running = false; this.failed = true; this.push({ type: 'err', text: 'stream interrupted' }); };
    },
  };
}

// Navbar omnisearch dropdown: open/close + keyboard nav over the HTMX-rendered result rows.
// Visibility is driven by results arriving (htmx:afterSwap, wired in x-init on the form) and by
// focus; we never build markup here — the server owns the dropdown body.
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

// Clusters tab: a pannable/zoomable canvas where you seed a central group and grow a tree outward.
// Each node's next ring = library tracks nearest its pinned-path centroid, pushed away from pruned
// tracks (server: POST /clusters/expand). Tree state lives here; the server stays stateless. Node
// positions are owned by a live d3-force simulation (link + charge + collide) so the graph lays
// itself out without overlap and re-settles as you grow/prune — d3 mutates each node's x/y in place,
// which Alpine renders reactively.
function clusterCanvas() {
  const WORLD = 8000, CENTER = WORLD / 2;   // big fixed world; we pan/zoom a transform over it
  const LINK_D = 215, NODE_R = 128;         // spoke length; collision radius (#14: roomier layout)
  return {
    WORLD,
    nodes: [], nextId: 1, rootId: null,
    query: '', results: [], seedSel: -1, genreSel: -1,   // dropdown keyboard cursors
    playlistName: '', includeCentral: true, saveMode: 'all',   // 'all' non-hidden | 'trunk' only
    journey: 'auto', journeyName: 'Pick for me', journeyOpen: false,   // DJ-journey ordering pick
    tx: 0, ty: 0, scale: 1,
    _pan: null, _drag: null, sim: null,
    explain: null,            // {childId, loading, data} — the "why this edge?" popover
    families: [], genres: [], allowedFamilies: [], genreOpen: false, genreQuery: '',   // #29 genre whitelist (families + sub-genres)
    trunk: [],                // #30 ids of grown nodes; the edge leading into each lights as trunk
    subHues: {}, _subHueN: 0,  // #14 parentId -> hue: every grown ring (sub-cluster) gets its own colour
    exhaustedIds: [],          // nodes whose + found nothing left under the active genre filter

    init() {
      // The central group is pinned at CENTER (fx/fy), so it anchors the whole graph: no centering
      // force needed and the view never drifts off it. Everything else is positioned by charge +
      // collide (no overlap) + link (spokes), so cards radiate around the centre.
      const d3 = window.d3;
      this.sim = d3.forceSimulation([])
        .force('charge', d3.forceManyBody().strength(-420).distanceMax(1600))
        .force('collide', d3.forceCollide(NODE_R).strength(0.95).iterations(2))
        .force('link', d3.forceLink([]).id(n => n.id).strength(0.5)
          // Spoke length grows with crowding: a busy ring needs longer spokes to fit around its
          // parent, and a child that has grown its OWN sub-cluster gets pushed further out so that
          // sub-cluster has clear room (recomputed on every _reheat).
          .distance(l => LINK_D
            + Math.min(220, Math.max(0, this.children(l.source.id).length - 4) * 28)
            + Math.min(320, this.descendants(l.target.id).size * 26)))
        .force('separate', this._clusterForce())   // keep distinct branches from overlapping
        .alphaDecay(0.035);
      this.sim.stop();                       // started on demand once there are nodes (see _reheat)
      this.$nextTick(() => { this.resetView(); this.$refs.seedInput && this.$refs.seedInput.focus(); });
      fetch('/clusters/genres').then(r => r.json())
        .then(d => { this.families = d.families || []; this.genres = d.genres || []; }).catch(() => {});
    },
    // Feed the current nodes/links to the sim and re-settle. `alpha` controls how much the layout is
    // allowed to move: ~0.9 for a real grow (spread the new ring), but a small value for a removal so
    // the graph barely shifts (#14: pruning shouldn't jiggle everything).
    _reheat(alpha = 0.9) {
      const links = this.nodes
        .filter(n => n.parentId != null && this.nodeById(n.parentId))
        .map(n => ({ source: n.parentId, target: n.id }));
      this.sim.nodes(this.nodes);
      this.sim.force('link').links(links);
      this.sim.alpha(alpha).restart();
    },
    // Update the sim's node/link sets WITHOUT re-energizing it — removed nodes leave the simulation
    // but everything else holds its exact position (#14: pruning must not jiggle the layout).
    _syncSim() {
      const links = this.nodes
        .filter(n => n.parentId != null && this.nodeById(n.parentId))
        .map(n => ({ source: n.parentId, target: n.id }));
      this.sim.nodes(this.nodes);
      this.sim.force('link').links(links);
    },
    // A d3 force that shoves cards of DIFFERENT branches apart when they crowd, so each sub-cluster
    // claims its own region instead of interleaving with a neighbour. Same-branch cards (and the
    // pinned centre) are left to the normal charge/collide/link forces.
    _clusterForce() {
      const self = this;
      let nodes = [];
      const SEP = 300;                       // target clearance between cards across branches
      function force(alpha) {
        const branch = nodes.map(n => (n.kind === 'central' ? null : self.branchId(n)));
        const k = alpha * 0.6;
        for (let i = 0; i < nodes.length; i++) {
          if (branch[i] == null) continue;
          const a = nodes[i];
          for (let j = i + 1; j < nodes.length; j++) {
            if (branch[j] == null || branch[j] === branch[i]) continue;   // same branch / centre: skip
            const b = nodes[j];
            let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy;
            if (d2 === 0) { dx = (i - j); dy = 1; d2 = dx * dx + 1; }
            if (d2 < SEP * SEP) {
              const d = Math.sqrt(d2), push = (SEP - d) / d * k;
              const fx = dx * push, fy = dy * push;
              a.vx -= fx; a.vy -= fy;
              b.vx += fx; b.vy += fy;
            }
          }
        }
      }
      force.initialize = (n) => { nodes = n; };
      return force;
    },

    // --- search / seeding ---
    async search() {
      const q = this.query.trim();
      if (!q) { this.results = []; this.seedSel = -1; return; }
      try {
        const r = await fetch('/clusters/search?q=' + encodeURIComponent(q));
        this.results = await r.json();
      } catch (e) { this.results = []; }
      this.seedSel = -1;                       // reset the keyboard cursor on fresh results
    },
    // Arrow-key navigation for the seed-search dropdown.
    seedMove(d) {
      if (!this.results.length) return;
      this.seedSel = (this.seedSel + d + this.results.length) % this.results.length;
    },
    seedChoose() {
      const r = this.results[this.seedSel >= 0 ? this.seedSel : 0];
      if (r) this.addSeed(r);
    },
    // Every selection lands in ONE central group (artist + artist + song + …), pinned at the centre.
    // Its centroid is the union of all its seeds' keys. The FIRST seed grows the opening ring; each
    // ADDED seed refines the core's taste direction (#12) — it re-ranks the whole tree in place
    // rather than bolting on another ring of cards.
    async addSeed(r) {
      this.query = ''; this.results = [];
      let root = this.rootId != null ? this.nodeById(this.rootId) : null;
      const fresh = !root;
      if (!root) {
        root = { id: this.nextId++, parentId: null, kind: 'central', state: 'central', depth: 0,
                 seeds: [], keys: [], key: null, vid: null,
                 x: CENTER, y: CENTER, fx: CENTER, fy: CENTER };
        this.rootId = root.id;
        this.nodes.push(root);
      }
      root.seeds.push({ label: r.label, kind: r.kind, keys: r.keys });
      root.keys = [...new Set(root.seeds.flatMap(s => s.keys))];   // combined central centroid
      if (!this.playlistName) this.playlistName = r.label + ' cluster';
      if (fresh) await this.grow(root.id); else await this.refineTree();
    },

    // --- tree growth ---
    // The shared ring fetch: tracks nearest `node`'s pinned-path centroid, pushed off the pruned set,
    // restricted to the genre-family whitelist (#29), minus everything already on the canvas.
    async expandRing(node, k) {
      try {
        const r = await fetch('/clusters/expand', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pos_keys: this.posKeys(node), neg_keys: this.prunedKeys(),
                                 exclude: this.allKeys(), k, allow_genres: this.allowedFamilies }),
        });
        return (await r.json()).ring || [];
      } catch (e) { return []; }
    },
    async grow(nodeId) {
      const node = this.nodeById(nodeId);
      if (!node || node.state === 'pruned') return;
      const ring = await this.expandRing(node, 6);
      if (!ring.length) {
        // Nothing left to add. Under a genre filter that means the genre's pool is spent here — flag
        // the node so its + greys out with an explanation (otherwise it just looks like a dead button).
        if (this.allowedFamilies.length && !this.exhaustedIds.includes(nodeId)) {
          this.exhaustedIds = [...this.exhaustedIds, nodeId];
        }
        return;
      }
      // #30 Growing THROUGH a node marks the path to it as trunk: clicking + on B lights the edge
      // center→B, and growing on down the branch keeps lighting each step. (The centre has no
      // incoming edge, so it never needs to join.)
      if (node.kind !== 'central' && !this.trunk.includes(nodeId)) this.trunk = [...this.trunk, nodeId];
      this.addChildren(node, ring);
    },
    // Re-rank every grown ring against the (now updated) centroids — refine, don't add (#12). Cards
    // you've shaped the tree with survive: pruned markers, drag-pinned cards, and any card you've
    // already grown beneath (a branch). Only the loose leaf cards get swapped for fresher picks, so
    // the card COUNT holds steady while the suggestions tighten around the refined core.
    async refineTree() {
      const parents = this.nodes.filter(n => this.children(n.id).length);
      for (const parent of parents) {
        if (!this.nodeById(parent.id)) continue;                 // a prior iteration may have changed things
        const kids = this.children(parent.id);
        const keep = kids.filter(k => k.state === 'pruned' || k.fx != null || this.children(k.id).length);
        const slots = kids.length - keep.length;
        if (slots <= 0) continue;
        const dropIds = new Set(kids.filter(k => !keep.includes(k)).map(k => k.id));
        this.nodes = this.nodes.filter(n => !dropIds.has(n.id));
        this.trunk = this.trunk.filter(id => !dropIds.has(id));
        if (this.explain && dropIds.has(this.explain.childId)) this.explain = null;
        this.addChildren(parent, await this.expandRing(parent, slots));
      }
    },

    // --- #29 genre whitelist (autosuggest combo: families AND sub-genres) ---
    // The full pick list: coarse families first, then individual genres; a name shown once (a family
    // and a like-named genre collapse to one token — picking it matches either).
    genreOptions() {
      const byName = new Map();
      for (const f of this.families) byName.set(f.family.toLowerCase(), { name: f.family, kind: 'family', n: f.n });
      for (const g of this.genres) {
        const key = g.genre.toLowerCase();
        if (!byName.has(key)) byName.set(key, { name: g.genre, kind: 'genre', n: g.n });
      }
      return [...byName.values()];
    },
    // Options matching what you've typed, minus those already picked (capped for a tidy dropdown).
    genreSuggest() {
      const q = (this.genreQuery || '').trim().toLowerCase();
      return this.genreOptions()
        .filter(o => !this.allowedFamilies.includes(o.name) && (!q || o.name.toLowerCase().includes(q)))
        .slice(0, 10);
    },
    // The genre filter PRUNES off-genre leaves (reversibly) and constrains future grows — it does NOT
    // refetch to refill rings, so the genre's pool stays available for + to grow into.
    _genreChanged() { this.exhaustedIds = []; this.applyGenrePrune(); },   // pool changed; re-prune off-genre
    pickFamily(fam) {
      if (!this.allowedFamilies.includes(fam)) this.allowedFamilies = [...this.allowedFamilies, fam];
      this.genreQuery = ''; this.genreSel = -1;
      this._genreChanged();
    },
    // Arrow-key navigation for the genre combo dropdown.
    genreMove(d) {
      const n = this.genreSuggest().length;
      if (n) this.genreSel = (this.genreSel + d + n) % n;
    },
    genreChoose() {                            // Enter selects the highlighted (or first) suggestion
      const opts = this.genreSuggest();
      const o = opts[this.genreSel >= 0 ? this.genreSel : 0];
      if (o) this.pickFamily(o.name);
    },
    removeFamily(fam) { this.allowedFamilies = this.allowedFamilies.filter(f => f !== fam); this._genreChanged(); },
    popFamily() {                              // Backspace on an empty field removes the last chip
      if (this.allowedFamilies.length) this.allowedFamilies = this.allowedFamilies.slice(0, -1);
      this._genreChanged();
    },
    clearFamilies() { this.allowedFamilies = []; this._genreChanged(); },
    // A track matches the active genre whitelist if a chosen token is its exact genre OR its family.
    // No filter ⇒ everything matches. Untagged tracks never match while a filter is on (#29).
    genreMatches(n) {
      if (!this.allowedFamilies.length || n.kind !== 'track') return true;
      const toks = this.allowedFamilies.map(t => t.toLowerCase());
      return toks.includes((n.genre || '').toLowerCase()) || toks.includes((n.family || '').toLowerCase());
    },
    // The genre filter doesn't HIDE off-genre cards — it PRUNES them (the same struck-through removed
    // state as the ✕), so they stay visible (paths intact), drop out of the save, and you can bring any
    // back with the ✕. Only loose leaves are touched — the trunk and structural nodes are left alone.
    // gpruned marks a prune the FILTER made, so flipping/clearing the filter can undo exactly those
    // (a card you ✕'d or kept by hand stays as you left it).
    applyGenrePrune() {
      for (const n of this.nodes) {
        if (n.kind !== 'track' || this.trunk.includes(n.id) || this.children(n.id).length) continue;
        const match = this.genreMatches(n);
        if (!match && n.state === 'neutral') { n.state = 'pruned'; n.gpruned = true; }
        else if (match && n.state === 'pruned' && n.gpruned) { n.state = 'neutral'; n.gpruned = false; }
      }
    },
    addChildren(parent, ring) {
      if (!ring.length) return;
      // This ring is one sub-cluster; give it its own hue the first time it's grown (#14). Golden-angle
      // by creation order ⇒ consecutive sub-clusters (e.g. down a trunk) step to clearly distinct colours.
      if (!(parent.id in this.subHues)) this.subHues[parent.id] = Math.round((this._subHueN++ * 137.508) % 360);
      ring.forEach((t, i) => {              // spawn near the parent; the sim spreads them out
        this.nodes.push({
          id: this.nextId++, parentId: parent.id, kind: 'track', key: t.key,
          label: t.title, sub: t.artist, thumbnail: t.thumbnail, vid: t.video_id,
          genre: t.genre || '', family: t.family || '',     // for the genre filter (#29)
          state: 'neutral', depth: parent.depth + 1,
          x: parent.x + Math.cos(i) * 30, y: parent.y + Math.sin(i) * 30 + 40,
        });
      });
      this._reheat();
    },
    prune(id) {
      const n = this.nodeById(id); if (!n || n.kind === 'central') return;
      // Toggling state changes only the card's look, not the layout — so don't restart the sim at
      // all (#14: no jiggle). Un-prune is a pure visual flip. A hand toggle clears gpruned so the
      // genre filter won't silently flip it back.
      if (n.state === 'pruned') { n.state = 'neutral'; n.gpruned = false; return; }
      n.state = 'pruned'; n.gpruned = false;           // pruning terminates the branch...
      this.trunk = this.trunk.filter(t => t !== id);   // ...a pushed-away node is no longer trunk
      const kill = this.descendants(id);              // ...so drop anything grown below it
      if (!kill.size) return;                          // pruning a leaf: nothing moves, nothing to do
      if (this.explain && kill.has(this.explain.childId)) this.explain = null;
      this.nodes = this.nodes.filter(x => !kill.has(x.id));
      this.trunk = this.trunk.filter(t => !kill.has(t));
      this._syncSim();                                 // drop the removed nodes without re-energizing — no jiggle
    },
    play(n) {                              // open the track on YouTube Music, reusing one named tab
      if (n.vid) window.open('https://music.youtube.com/watch?v=' + n.vid, 'ytPlayerTab');
    },

    // --- derived keys ---
    nodeById(id) { return this.nodes.find(n => n.id === id); },
    children(id) { return this.nodes.filter(n => n.parentId === id); },
    // The top-level branch a node belongs to: the first-ring ancestor whose parent is the centre.
    // Used to keep whole branches from overlapping (separate force).
    branchId(n) {
      let cur = n;
      while (cur && cur.parentId != null && cur.parentId !== this.rootId) cur = this.nodeById(cur.parentId);
      return cur ? cur.id : null;
    },
    // #14 colour-coding: a card takes the hue of the SUB-CLUSTER it belongs to — i.e. the grown ring
    // that spawned it, keyed by its parent. Each ring has its own vivid hue (assigned in addChildren),
    // so three sub-clusters down a trunk read as three clearly different colours, not shades of one.
    nodeStyle(n) {
      if (n.kind === 'central' || n.parentId == null) return '';
      const hue = this.subHues[n.parentId];
      if (hue == null) return '';
      return `--node-hue:${hue};--node-sat:70%;--node-light:60%;`;
    },
    // #30 The "trunk" is the spine you GROW out (via the + button — see grow()): each node you grow
    // through lights the bright-green line leading into it, so the path you explored glows edge by edge.
    isTrunkNode(id) { return this.trunk.includes(id); },
    isTrunkEdge(n) { return n.parentId != null && this.trunk.includes(n.id); },
    // Two SVG <path>s: the faint branches and the bright trunk over them (Alpine can't reliably make
    // per-edge <line> elements inside <svg> — namespace issues — so each is one path on a static node).
    edgePath() { return this._edgeD(false); },
    trunkPath() { return this._edgeD(true); },
    _edgeD(trunk) {
      let d = '';
      for (const n of this.nodes) {
        if (n.parentId == null) continue;
        const p = this.nodeById(n.parentId); if (!p) continue;
        if (this.isTrunkEdge(n) !== trunk) continue;
        d += `M${p.x} ${p.y}L${n.x} ${n.y}`;
      }
      return d;
    },
    // Each non-root node owns one edge (to its parent); its clickable midpoint dot reveals WHY the
    // connection was made. Recomputed every render so the dots ride the live force simulation.
    edges() {
      const out = [];
      for (const n of this.nodes) {
        if (n.parentId == null) continue;
        const p = this.nodeById(n.parentId); if (!p) continue;
        out.push({ id: n.id, x: (p.x + n.x) / 2, y: (p.y + n.y) / 2 });
      }
      return out;
    },
    // Open the "why this edge?" popover: ask the server to explain the child against its pinned path
    // (the same positive centroid that grew it — central group + every ancestor up the branch).
    async openExplain(childId) {
      const child = this.nodeById(childId); if (!child) return;
      const parent = this.nodeById(child.parentId); if (!parent) return;
      if (this.explain && this.explain.childId === childId) { this.explain = null; return; }  // toggle off
      this.explain = { childId, loading: true, data: null };
      try {
        const r = await fetch('/clusters/explain', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: child.key, path_keys: this.posKeys(parent) }),
        });
        const data = await r.json();
        if (this.explain && this.explain.childId === childId) this.explain = { childId, loading: false, data };
      } catch (e) {
        if (this.explain && this.explain.childId === childId) this.explain = null;
      }
    },
    closeExplain() { this.explain = null; },
    // Live midpoint of the explained edge, so the popover tracks the node as the sim settles.
    explainAnchor() {
      if (!this.explain) return { x: 0, y: 0 };
      const n = this.nodeById(this.explain.childId); if (!n) return { x: 0, y: 0 };
      const p = this.nodeById(n.parentId); if (!p) return { x: n.x, y: n.y };
      return { x: (p.x + n.x) / 2, y: (p.y + n.y) / 2 };
    },
    centralKeys() { const r = this.rootId != null ? this.nodeById(this.rootId) : null; return r ? r.keys : []; },
    // The seed labels (artist/playlist/song names) you built the cluster from — for its recipe's
    // "Made from" line on save (#15).
    seedLabels() {
      const r = this.rootId != null ? this.nodeById(this.rootId) : null;
      return r ? [...new Set(r.seeds.map(s => s.label))] : [];
    },
    // Only SONG seeds are concrete "central tracks" worth offering to fold into the saved playlist —
    // an artist/playlist seed steers the centroid but isn't a track you explicitly picked.
    centralSongKeys() {
      const r = this.rootId != null ? this.nodeById(this.rootId) : null;
      return r ? r.seeds.filter(s => s.kind === 'song').flatMap(s => s.keys) : [];
    },
    prunedKeys() { return this.nodes.filter(n => n.state === 'pruned').map(n => n.key); },
    keepKeys() {
      return this.nodes.filter(n => n.kind === 'track' && n.state !== 'pruned').map(n => n.key);
    },
    // The spine you grew out: only the trunk nodes (the ones you clicked + on), non-pruned.
    trunkKeys() {
      return this.nodes.filter(n => n.kind === 'track' && n.state !== 'pruned' && this.trunk.includes(n.id))
        .map(n => n.key);
    },
    // What a Save actually writes, per the save-bar toggle.
    saveKeys() { return this.saveMode === 'trunk' ? this.trunkKeys() : this.keepKeys(); },
    allKeys() {
      const s = new Set(this.centralKeys());
      this.nodes.forEach(n => { if (n.key) s.add(n.key); });
      return [...s];
    },
    // Growing a card IS the positive signal, so a node's children aim at the central group plus
    // every track on the path you grew through to reach it (no separate "pin").
    posKeys(node) {
      const keys = new Set(this.centralKeys());
      for (let cur = node; cur && cur.kind === 'track'; cur = this.nodeById(cur.parentId)) {
        keys.add(cur.key);
      }
      return [...keys];
    },
    descendants(id) {
      const out = new Set(); const stack = [id];
      while (stack.length) {
        const p = stack.pop();
        this.nodes.filter(n => n.parentId === p).forEach(c => { out.add(c.id); stack.push(c.id); });
      }
      return out;
    },

    // --- pan / zoom ---
    worldStyle() { return `transform: translate(${this.tx}px, ${this.ty}px) scale(${this.scale}); transform-origin: 0 0;`; },
    onWheel(e) {
      const rect = e.currentTarget.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      const factor = Math.exp(-e.deltaY * 0.0015);
      const ns = Math.min(2.5, Math.max(0.2, this.scale * factor));
      this.tx = mx - ((mx - this.tx) / this.scale) * ns;
      this.ty = my - ((my - this.ty) / this.scale) * ns;
      this.scale = ns;
    },
    _worldPt(e) {                            // screen → world coords (undo pan + zoom)
      const rect = document.getElementById('cluster-canvas').getBoundingClientRect();
      return { x: (e.clientX - rect.left - this.tx) / this.scale,
               y: (e.clientY - rect.top - this.ty) / this.scale };
    },
    startNodeDrag(n, e) {                    // pointerdown on a card body — grab it, not the canvas
      const p = this._worldPt(e);
      this._drag = { id: n.id, moved: false, ox: p.x - n.x, oy: p.y - n.y, sx: e.clientX, sy: e.clientY };
    },
    onPanStart(e) {
      if (e.target.closest('.cluster-node, .cluster-zoombar, .edge-dot, .cluster-explain')) return;   // let nodes/buttons/dots get clicks
      this._pan = { x: e.clientX, y: e.clientY };
    },
    onPanMove(e) {
      if (this._drag) {                      // pointer down on a card
        const n = this.nodeById(this._drag.id); if (!n) return;
        if (!this._drag.moved) {             // stay a CLICK until the pointer travels a few px (#30:
          const dx = e.clientX - this._drag.sx, dy = e.clientY - this._drag.sy;   // so a click reliably
          if (dx * dx + dy * dy < 25) return;                                     // toggles the trunk)
          this._drag.moved = true;
        }
        const p = this._worldPt(e);          // past the threshold: it's a drag — pin under the pointer
        n.fx = p.x - this._drag.ox; n.fy = p.y - this._drag.oy;
        n.x = n.fx; n.y = n.fy;
        this.sim.alpha(0.2).restart();
        return;
      }
      if (!this._pan) return;
      this.tx += e.clientX - this._pan.x; this.ty += e.clientY - this._pan.y;
      this._pan = { x: e.clientX, y: e.clientY };
    },
    onPanEnd() {
      if (this._drag) {
        const n = this.nodeById(this._drag.id);
        // a pure click (no move) leaves a track free to flow; a real drag pins it where dropped.
        if (n && !this._drag.moved && n.kind !== 'central') { n.fx = null; n.fy = null; }
        this._drag = null;
      }
      this._pan = null;
    },
    zoomBy(f) {
      const el = document.getElementById('cluster-canvas'); const rect = el.getBoundingClientRect();
      const cx = rect.width / 2, cy = rect.height / 2;
      const ns = Math.min(2.5, Math.max(0.2, this.scale * f));
      this.tx = cx - ((cx - this.tx) / this.scale) * ns;
      this.ty = cy - ((cy - this.ty) / this.scale) * ns;
      this.scale = ns;
    },
    resetView() { this._centerWorld(CENTER, CENTER, 1); },
    _centerWorld(wx, wy, scale) {
      const el = document.getElementById('cluster-canvas'); if (!el) return;
      const rect = el.getBoundingClientRect();
      this.scale = scale;
      this.tx = rect.width / 2 - wx * scale;
      this.ty = rect.height / 2 - wy * scale;
    },
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
