function rowSort(pid) {
  // Generic click-to-sort for a static-row table; reorders <tr class="srow"> by data-<key>.
  // Numeric when both values parse as numbers, else locale string compare.
  // Also hosts playlist editing dialogs, invoked by the shared song menu.
  return {
    pid: pid, key: '', dir: 1,
    _sortable: null,
    destroy() { this._sortable?.destroy(); },
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
    ind(k) { return this.key === k ? (this.dir === 1 ? 'is-ascending' : 'is-descending') : ''; },

    // Open the modal and let htmx fetch + render the results (server builds the list; we only own the
    // modal open/close + loading flag). The "Add" button hx-includes the checked results.
    findAlternates(vid, title) {
      this.altOpen = true; this.altLoading = true; this.altTitle = title;
      htmx.ajax('GET', `/playlist/${this.pid}/alternates?video_id=${encodeURIComponent(vid)}`,
        { target: '#alt-results', swap: 'innerHTML' }).finally(() => { this.altLoading = false; });
    },

    // remove-track confirmation modal
    rmOpen: false, rmBusy: false, rmErr: '', rmVid: '', rmTitle: '',
    removeTrack(vid, title) {
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
      this._sortable?.destroy();
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
    ind(k) { return this.key === k ? (this.dir === 1 ? 'is-ascending' : 'is-descending') : ''; },
  };
}
