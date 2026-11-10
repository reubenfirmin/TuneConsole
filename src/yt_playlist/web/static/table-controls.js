function rowSort(pid) {
  // Generic click-to-sort for a static-row table; reorders <tr class="srow"> by data-<key>.
  // Numeric when both values parse as numbers, else locale string compare.
  // Also hosts playlist editing dialogs, invoked by the shared song menu.
  let alternateRequest = null;
  return {
    pid: pid, key: '', dir: 1,
    _sortable: null,
    destroy() { this.closeAlternates(); this._sortable?.destroy(); },
    // alternate-versions modal
    altOpen: false, altLoading: false, altAdding: false, altTitle: '', altVid: '', altStatus: '', altHasResults: false,
    placementStatus: '',
    saveRecovery(state) {
      if (!state) return;
      const results = document.querySelector('#alt-results');
      state.playlistEdits = {pid: this.pid, placement: this.placementStatus,
        remove: this.rmOpen ? {vid: this.rmVid, title: this.rmTitle, status: this.rmErr} : null,
        alternate: this.altOpen ? {vid: this.altVid, title: this.altTitle, status: this.altStatus,
          html: results?.innerHTML || '',
          checked: Array.from(results?.querySelectorAll('input[name="track"]:checked') || [], input => input.dataset.videoId)} : null};
    },
    restoreRecovery(state) {
      const saved = state?.playlistEdits;
      if (saved?.pid !== this.pid) return;
      const exists = vid => !!this.$refs.body.querySelector(`tr.srow[data-vid="${CSS.escape(vid)}"]`);
      this.placementStatus = saved.placement;
      if (saved.remove && exists(saved.remove.vid)) {
        this.rmVid = saved.remove.vid; this.rmTitle = saved.remove.title;
        this.rmErr = saved.remove.status; this.rmOpen = true;
      }
      if (saved.alternate && exists(saved.alternate.vid)) {
        const alternate = saved.alternate, results = document.querySelector('#alt-results');
        this.altVid = alternate.vid; this.altTitle = alternate.title;
        this.altStatus = alternate.status; this.altOpen = true;
        results.innerHTML = alternate.html;
        const checked = new Set(alternate.checked);
        for (const input of results.querySelectorAll('input[name="track"]')) input.checked = checked.has(input.dataset.videoId);
        // Alpine stamps the teleported dialog after the page's HTMX scan.
        // Rebind its submit control as well as the restored result inputs.
        this.$nextTick(() => htmx.process(results.closest('[role="dialog"]')));
        this.altHasResults = !!results.querySelector('input[name="track"]');
      }
    },
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

    // Only a successful read replaces results. Failed refreshes retain checked
    // versions, and an explicit retry owns a fresh bounded request.
    async findAlternates(vid, title) {
      if (this.altAdding) return;
      alternateRequest?.abort();
      const controller = new AbortController();
      alternateRequest = controller;
      const results = document.querySelector('#alt-results');
      if (this.altVid !== vid) {
        results?.replaceChildren();
        this.altHasResults = false;
      }
      this.altVid = vid; this.altTitle = title; this.altStatus = '';
      this.altOpen = true; this.altLoading = true;
      const current = () => alternateRequest === controller && this.altOpen;
      const cancel = () => controller.abort();
      const timer = setTimeout(cancel, 20000);
      window.addEventListener('pagehide', cancel, {once: true});
      window.addEventListener('app-page-leaving', cancel, {once: true});
      try {
        const response = await fetch(`/playlist/${this.pid}/alternates?video_id=${encodeURIComponent(vid)}`,
          {signal: controller.signal});
        const body = await response.text();
        controller.signal.throwIfAborted();
        if (!current()) return;
        if (response.status === 422) {
          const messages = {
            playlist_missing: 'Playlist no longer exists',
            playlist_song_missing: 'This song is no longer in the playlist.',
            playlist_account_disconnected: "That playlist's account isn't connected."
          };
          const code = JSON.parse(body).validation;
          this.altStatus = Object.hasOwn(messages, code) ? messages[code] : 'Search not completed. Try again.';
          return;
        }
        if (!response.ok || response.headers.get('X-Alternate-Results') !== 'true') throw new Error('Alternate read unavailable');
        const fragment = document.createElement('template');
        fragment.innerHTML = body;
        const content = fragment.content.querySelector('[data-alternate-source]');
        if (!content || content.dataset.alternateSource !== vid) throw new Error('Alternate receipt unavailable');
        const selected = new Set(Array.from(results.querySelectorAll('input[name="track"]:checked'), input => input.dataset.videoId));
        for (const input of content.querySelectorAll('input[name="track"]')) input.checked = selected.has(input.dataset.videoId);
        results.replaceChildren(content);
        htmx.process(results);
        this.altHasResults = !!results.querySelector('input[name="track"]');
      } catch (error) {
        if (current()) {
          console.debug('[Alternate versions] read unavailable', error);
          this.altStatus = 'Search not completed. Try again.';
        }
      } finally {
        clearTimeout(timer);
        window.removeEventListener('pagehide', cancel);
        window.removeEventListener('app-page-leaving', cancel);
        if (alternateRequest === controller) {
          alternateRequest = null;
          this.altLoading = false;
        }
      }
    },
    closeAlternates() {
      this.altOpen = false; this.altLoading = false;
      alternateRequest?.abort(); alternateRequest = null;
    },
    alternateAdded(event) {
      this.altAdding = false;
      if (event.detail.xhr.status >= 200 && event.detail.xhr.status < 300) {
        this.altStatus = '';
        this.closeAlternates();
      } else this.altStatus = 'Addition was not confirmed. Review the playlist before adding again.';
    },

    // remove-track confirmation modal
    rmOpen: false, rmBusy: false, rmErr: '', rmVid: '', rmTitle: '',
    removeTrack(vid, title) {
      this.rmVid = vid; this.rmTitle = title; this.rmErr = ''; this.rmOpen = true;
    },
    async confirmRemove() {
      if (this.rmBusy) return;
      this.rmBusy = true;
      this.rmErr = '';
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(this.rmVid)}"]`);
      // Only a confirmed success removes the row and closes this action. A
      // failed receipt leaves it open for the GET reconciliation owner.
      try {
        await htmx.ajax('POST', `/playlist/${this.pid}/remove-track`,
          { source: this.$refs.removeSubmit, values: { video_id: this.rmVid }, target: tr, swap: 'outerHTML' });
      } catch (e) { /* The request event retains the action's unresolved status. */ }
      finally { this.rmBusy = false; }
    },
    removeFinished(event) {
      this.rmBusy = false;
      if (event.detail.xhr.status >= 200 && event.detail.xhr.status < 300) this.rmOpen = false;
      else this.rmErr = 'Removal was not confirmed. Review the playlist before removing again.';
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
