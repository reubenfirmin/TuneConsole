// Launchers carry song data only. Every view uses the same menu, dialog, and operations.
document.addEventListener('click', event => {
  const trigger = event.target.closest('[data-song]');
  if (!trigger) return;
  event.preventDefault();
  event.stopPropagation();
  window.dispatchEvent(new CustomEvent('song-menu-open', { detail: {
    trigger, song: JSON.parse(trigger.dataset.song), context: JSON.parse(trigger.dataset.songContext || '{}')
  }}));
});

function songActions() {
  // Keep DOM nodes and the originating Alpine scope outside reactive data.
  let trigger = null, owner = null, request = 0;
  return {
    song: {}, context: {}, mode: 'create', name: '', query: '', selected: '',
    playlists: [], multipleIdentities: false, loading: false, busy: false, error: '', result: null,
    artworkFailed: false,
    reasons: [['vibe', 'Wrong vibe'], ['era', 'Wrong era'], ['mainstream', 'Too mainstream'],
              ['own_it', 'Already know it'], ['artist', 'Not this artist']],
    show(detail) {
      if (this.$refs.dialog.open) return;
      const wasOpen = this.$refs.menu.matches(':popover-open');
      const same = trigger === detail.trigger;
      if (wasOpen) this.$refs.menu.hidePopover();
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
      if (wasOpen && same) return;
      trigger = detail.trigger;
      owner = Alpine.$data(trigger);
      this.song = detail.song;
      this.artworkFailed = false;
      this.context = detail.context;
      this.$nextTick(() => {
        const menu = this.$refs.menu, rect = trigger.getBoundingClientRect();
        menu.showPopover({ source: trigger });
        menu.style.left = Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8)) + 'px';
        const top = rect.bottom + menu.offsetHeight + 12 <= innerHeight
          ? rect.bottom + 4 : rect.top - menu.offsetHeight - 4;
        menu.style.top = Math.max(8, top) + 'px';
        trigger.setAttribute('aria-expanded', 'true');
        menu.querySelector('[role="menuitem"]').focus();
      });
    },
    menuToggled(event) {
      if (event.newState === 'closed' && trigger) trigger.setAttribute('aria-expanded', 'false');
    },
    closeMenu() {
      this.$refs.menu.hidePopover();
    },
    menuKey(event) {
      const items = [...this.$refs.menu.querySelectorAll('[role="menuitem"]')].filter(el => el.getClientRects().length);
      const index = items.indexOf(document.activeElement);
      let next;
      if (event.key === 'ArrowDown') next = (index + 1) % items.length;
      if (event.key === 'ArrowUp') next = (index - 1 + items.length) % items.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = items.length - 1;
      if (next !== undefined) { event.preventDefault(); items[next].focus(); }
      if (event.key === 'Escape' || event.key === 'Tab') {
        if (event.key === 'Escape') event.preventDefault();
        this.closeMenu();
        trigger?.focus();
      }
    },
    play() {
      this.closeMenu();
      tcPlay('https://music.youtube.com/watch?v=' + encodeURIComponent(this.song.video_id));
    },
    similar() {
      this.closeMenu();
      const pid = this.context.playlist ? owner.pid : this.context.pid;
      const query = pid ? '?pid=' + encodeURIComponent(pid) : '';
      htmx.ajax('GET', '/track/' + encodeURIComponent(this.song.video_id) + '/similar' + query,
        { target: '#similar-modal', swap: 'innerHTML' });
    },
    localAction(action) {
      this.closeMenu();
      owner[action](this.song.video_id, this.song.title);
    },
    async mood(direction) {
      this.closeMenu();
      const target = owner;
      await htmx.ajax('POST', '/recs/mood', { values: {
        keys: JSON.stringify([this.context.mood_key]), dir: direction
      }, swap: 'none' });
      target.mood = direction;
    },
    dismiss(reason) {
      this.closeMenu();
      const values = { item: this.context.key, surface: 'suggest', scope: this.context.pid,
        kind: 'dismiss', reason };
      if (reason === 'artist') values.axis = 'artist:' + this.song.artist;
      htmx.ajax('POST', '/recs/feedback', { values, target: trigger.closest('.tile'), swap: 'outerHTML' });
    },
    async openPlaylist(mode) {
      this.closeMenu();
      this.mode = mode;
      this.name = this.song.title;
      this.query = ''; this.selected = ''; this.playlists = [];
      this.error = ''; this.result = null; this.busy = false;
      this.loading = mode === 'add';
      const current = ++request;
      this.$refs.dialog.showModal();
      this.focusDialog(mode === 'create' ? 'name' : 'search');
      if (mode !== 'add') return;
      try {
        const data = await this.json('/songs/playlists?video_id=' + encodeURIComponent(this.song.video_id));
        if (current !== request) return;
        this.playlists = data.playlists;
        this.multipleIdentities = data.multiple_identities;
      } catch (error) {
        if (current === request) this.error = error.message;
      } finally {
        if (current === request) {
          this.loading = false;
          this.focusDialog('search');
        }
      }
    },
    focusDialog(ref) {
      // Alpine reveals x-show elements on the next frame, after its reactive tick.
      this.$nextTick(() => requestAnimationFrame(() => {
        if (!this.$refs.dialog.open) return;
        this.$refs[ref].focus();
        if (ref === 'name') {
          this.$refs.name.select();
          this.$refs.name.scrollLeft = 0;
        }
      }));
    },
    closeDialog() {
      if (this.busy) return;
      request++;
      this.$refs.dialog.close();
      trigger?.focus();
      if (this.result?.url === location.pathname) location.reload();
    },
    backdropClick(event) {
      if (event.target !== this.$refs.dialog) return;
      const rect = this.$refs.dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right ||
          event.clientY < rect.top || event.clientY > rect.bottom) this.closeDialog();
    },
    trapFocus(event) {
      const items = [...this.$refs.dialog.querySelectorAll('button, input, a[href]')]
        .filter(el => !el.disabled && el.getClientRects().length);
      const first = items[0], last = items.at(-1);
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    },
    matches() {
      const query = this.query.trim().toLocaleLowerCase();
      return this.playlists.filter(p => (p.title + ' ' + p.identity).toLocaleLowerCase().includes(query));
    },
    async json(url, body) {
      const response = await fetch(url, body === undefined ? {} : {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Couldn’t save this song. Please try again.');
      return data;
    },
    async submit() {
      if (this.busy || this.loading) return;
      this.busy = true; this.error = '';
      try {
        this.result = await this.json('/songs/' + (this.mode === 'create' ? 'create-playlist' : 'add-to-playlist'), {
          song: this.song, ...(this.mode === 'create' ? { name: this.name.trim() } : { playlist_id: Number(this.selected) })
        });
        this.focusDialog('resultLink');
      } catch (error) {
        this.error = error.message || 'Couldn’t save this song. Please try again.';
      } finally {
        this.busy = false;
      }
    }
  };
}
