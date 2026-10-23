// Setup → Enrichment tab: drag to reorder metadata providers (SortableJS, same as playlist rows),
// toggle to enable/disable. `providers` carries each {name, requires}; a provider must stay after
// the one it requires (AcousticBrainz after MusicBrainz), enforced live in onMove so the DOM never
// reaches an invalid state. Order + enabled flags persist to /setup/enrichment (config only).
function enrichmentTab(providers) {
  const reqs = providers.filter(p => p.requires).map(p => [p.name, p.requires]);
  return {
    _sortable: null,
    destroy() { this._sortable?.destroy(); },
    initSortable() {
      const list = this.$refs.list;
      if (!list || typeof Sortable === 'undefined') return;
      this._sortable?.destroy();
      this._sortable = Sortable.create(list, {
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

