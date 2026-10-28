function playlistsTab(rows) {
  // Remember the particular row, since New playlists repeats rows from the library below.
  let lastPickedRow = null;
  return {
    rows, activityScale: 0, sel: {}, sortKey: 'title', sortDir: 1, split: false, busy: false,
    groupModal: false, groupName: '', delModal: false, collapsed: { New: false, Generated: true },
    init() {
      this.refreshActivityScale();
      // Alpine watches nested row changes too, including live listen-count updates.
      this.$watch('rows', () => this.refreshActivityScale());
      // remember view preferences across reloads (the tab reloads after group/delete)
      try {
        this.split = localStorage.getItem('pl.split') === '1';
        this.sortKey = localStorage.getItem('pl.sortKey') || 'title';
        this.sortDir = +localStorage.getItem('pl.sortDir') || 1;
        const savedCollapsed = JSON.parse(localStorage.getItem('pl.collapsed') || '{}');
        this.collapsed = { New: false, Generated: true,
          ...(savedCollapsed && typeof savedCollapsed === 'object' ? savedCollapsed : {}) };
      } catch (e) {}
      // Both sections share the same expanded slot; older preferences may have both open.
      if (!this.collapsed.New && !this.collapsed.Generated && this.newRows().length) {
        this.collapsed.Generated = true;
      }
      this.$watch('split', v => { try { localStorage.setItem('pl.split', v ? '1' : '0'); } catch (e) {} });
      this.$watch('sortKey', v => { try { localStorage.setItem('pl.sortKey', v); } catch (e) {} });
      this.$watch('sortDir', v => { try { localStorage.setItem('pl.sortDir', v); } catch (e) {} });
    },
    selected() { return this.rows.filter(r => this.sel[r.id]); },
    count() { return this.selected().length; },
    toggle(id) { this.sel[id] = !this.sel[id]; },
    // checkbox (or its cell) clicked: shift extends the anchor row's state across the visible range
    rowCheck(r, ev) {
      const row = ev?.currentTarget.closest('tr');
      if (ev?.shiftKey && lastPickedRow && lastPickedRow !== row) {
        const visible = [...this.$root.querySelectorAll('tr[data-playlist-id]')]
          .filter(el => el.getClientRects().length);
        const i = visible.indexOf(lastPickedRow), j = visible.indexOf(row);
        if (i !== -1 && j !== -1) {
          const on = !!this.sel[lastPickedRow.dataset.playlistId];
          visible.slice(Math.min(i, j), Math.max(i, j) + 1)
            .forEach(el => { this.sel[el.dataset.playlistId] = on; });
          lastPickedRow = row;
          return;
        }
      }
      this.toggle(r.id);
      lastPickedRow = row;
    },
    sortBy(key) {
      if (this.sortKey === key) { this.sortDir = -this.sortDir; }
      else { this.sortKey = key; this.sortDir = ['count', 'listens', 'last'].includes(key) ? -1 : 1; }
    },
    ind(key) { return this.sortKey === key ? (this.sortDir === 1 ? 'is-ascending' : 'is-descending') : ''; },
    refreshActivityScale() {
      const max = this.rows.reduce((value, row) => Math.max(value, row.listens || 0), 0);
      this.activityScale = Math.log1p(max);
    },
    activity(r) {
      return r.listens && this.activityScale ? Math.log1p(r.listens) / this.activityScale : 0;
    },
    activityLabel(r) {
      const n = r.listens || 0;
      return n.toLocaleString() + ' playlist listening session' + (n === 1 ? '' : 's') + ' (partial listens count)';
    },
    cmp(a, b) {
      const k = this.sortKey, numeric = (k === 'count' || k === 'listens' || k === 'last');
      const r = numeric ? ((a[k] || 0) - (b[k] || 0))
                        : String(a[k] || '').localeCompare(String(b[k] || ''));
      return (r ? r * this.sortDir : a.title.localeCompare(b.title));   // stable tiebreak by title
    },
    sorted() { return [...this.rows].sort((a, b) => this.cmp(a, b)); },
    toggleSection(name) {
      this.collapsed[name] = !this.collapsed[name];
      if (!this.collapsed[name]) this.collapsed[name === 'New' ? 'Generated' : 'New'] = true;
      try { localStorage.setItem('pl.collapsed', JSON.stringify(this.collapsed)); } catch (e) {}
    },
    newRows() {
      return this.rows.filter(r => r.is_new)
        .sort((a, b) => b.created - a.created || b.id - a.id);
    },
    // "Generated" is pinned into its own card above the table (see template), never in the sections.
    // Always newest-first by creation time (independent of the main table's column sort).
    genRows() {
      return this.rows.filter(r => r.is_generated)
        .sort((a, b) => (b.generated_created ?? b.created ?? 0) - (a.generated_created ?? a.created ?? 0)
          || b.id - a.id);
    },
    promote(r) {
      // graduate a Generated playlist into the library (out of the quarantine group), then reload
      fetch('/playlist/' + r.id + '/promote', { method: 'POST', headers: { 'HX-Request': 'true' } })
        .then(() => location.replace(location.href));
    },
    sections() {
      // the main table holds everything EXCEPT Generated; split partitions by group (Ungrouped last)
      const rest = this.sorted().filter(r => !r.is_generated);
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
      location.href = '/merge?ids=' + this.selected().map(r => r.id).join(',') + '&return=/playlists';
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
    copy() { htmx.ajax('POST', '/playlists/copy', { values: { ids: this.copyIds.join(','), name: this.copyName } }).catch(() => {}); },
    copyInto() { htmx.ajax('POST', '/playlists/copy-into', { values: { ids: this.copyIds.join(','), target: this.copyIntoTarget } }); },
    group() { htmx.ajax('POST', '/playlists/group', { values: { ids: this.selected().map(r => r.id).join(','), name: this.groupName } }); },
    // Delete keeps the modal up with a spinner: one remote YouTube call per playlist, so a batch
    // can take a while. `busy` blocks a second submit; success ends in a full page reload.
    remove() {
      if (this.busy) return;
      this.busy = true;
      htmx.ajax('POST', '/playlists/delete', { values: { ids: this.selected().map(r => r.id).join(',') } })
        .catch(() => { this.busy = false; });
    },
  };
}
function moveTab(fromId, toId, rows) {
  return {
    from: fromId, to: toId, rows, sel: {}, lastChecked: null,
    busy: false, progress: '', error: '',
    init() {
      this.$watch('from', () => { this.sel = {}; this.lastChecked = null; });
    },
    visibleIds() { return this.rows.filter(r => r.identity_id === this.from).map(r => r.id); },
    selectedIds() { return this.visibleIds().filter(id => this.sel[id]); },
    count() { return this.selectedIds().length; },
    allSelected() { return this.visibleIds().length > 0 && this.count() === this.visibleIds().length; },
    canMove() { return this.from != null && this.to != null && this.from !== this.to; },
    selectAll(on) {
      if (this.busy) return;
      this.sel = on ? Object.fromEntries(this.visibleIds().map(id => [id, true])) : {};
      this.lastChecked = null;
    },
    rowCheck(id, event) {
      if (this.busy) return;
      const on = !this.sel[id], ids = this.visibleIds();
      const start = ids.indexOf(this.lastChecked), end = ids.indexOf(id);
      if (event.shiftKey && start >= 0 && end >= 0) {
        ids.slice(Math.min(start, end), Math.max(start, end) + 1).forEach(pid => { this.sel[pid] = on; });
      } else {
        this.sel[id] = on;
      }
      this.lastChecked = id;
    },
    async transfer(copyOnly) {
      if (this.busy || !this.canMove() || !this.count()) return;
      const ids = this.selectedIds();
      this.busy = true;
      this.error = '';
      this.progress = `${copyOnly ? 'Copying' : 'Moving'} ${ids.length} playlist${ids.length === 1 ? '' : 's'}…`;
      try {
        await htmx.ajax('POST', '/move/bulk', {
          target: '#move-panel', swap: 'outerHTML',
          values: { ids: ids.join(','), source_identity: this.from,
            target_identity: this.to, copy_only: copyOnly ? '1' : '' },
        });
      } catch (_) {
        this.error = 'The transfer was interrupted. Check both identities before retrying; some playlists may have been copied or moved.';
      } finally {
        this.busy = false;
      }
    },
  };
}
function titleEditor(pid) {
  // Generated headings present a shorter name. Keep the exact escaped source
  // title on the editor root so editing never drops its type/date/version.
  return {
    pid: pid, editing: false, draft: '',
    originalTitle() { return this.$root.dataset.title ?? this.$refs.h1.textContent.trim(); },
    start() {
      this.draft = this.originalTitle();
      this.editing = true;
      this.$nextTick(() => { this.$refs.inp.focus(); this.$refs.inp.select(); });
    },
  };
}
