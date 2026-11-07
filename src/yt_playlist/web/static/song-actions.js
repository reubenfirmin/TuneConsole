function trackUrl(song) {
  const video = song?.video_id || song?.videoId || song?.vid;
  if (!video) return '';
  const query = new URLSearchParams();
  for (const field of ['title', 'artist', 'album', 'thumbnail', 'duration']) {
    if (song[field] !== undefined && song[field] !== null && song[field] !== '') query.set(field, song[field]);
  }
  if (song.album_browse || song.album_browse_id) query.set('album_browse', song.album_browse || song.album_browse_id);
  return '/track/' + encodeURIComponent(video) + (query.size ? '?' + query : '');
}

// Launchers carry song data only. Every view uses the same menu, dialog, and operations.
document.addEventListener('click', event => {
  const trigger = event.target.closest('[data-song]');
  if (!trigger) return;
  event.preventDefault();
  event.stopPropagation();
  window.dispatchEvent(new CustomEvent('song-menu-open', { detail: {
    trigger, song: JSON.parse(trigger.dataset.song), context: JSON.parse(trigger.dataset.songContext || '{}'),
    action: trigger.dataset.songAction
  }}));
});

// Refresh every visible copy without replacing rows, player state, or the page.
function applySongMetadata(videoId, metadata) {
  const { title, artist = '', genre = '', year = '' } = metadata;
  for (const button of document.querySelectorAll('[data-song]')) {
    const song = JSON.parse(button.dataset.song);
    if (song.video_id !== videoId) continue;
    button.dataset.song = JSON.stringify({ ...song, title, artist });
    button.setAttribute('aria-label', 'More actions for ' + title);
    const row = button.closest('tr, .gen-row, .rt-row, .tile');
    if (!row) continue;
    const setText = (selector, value) => {
      for (const node of row.querySelectorAll(selector)) {
        // Chart titles also contain an external-link icon.
        node.replaceChildren(document.createTextNode(value), ...node.children);
        if (node.hasAttribute('title')) node.title = value;
        if (node.closest('td[title]')) node.closest('td').title = value;
      }
    };
    setText('.ptitle, .gen-title, .rt-c-title, .tile-cap-title', title);
    setText('.alink[href^="/artist?"], .gen-artist, .rt-c-artist', artist);
    setText('.rt-c-genre', genre);
    setText('.rt-c-year', year || '');
    for (const link of row.querySelectorAll('a[href^="/artist?"]')) {
      link.href = '/artist?name=' + encodeURIComponent(artist);
    }
    for (const node of row.querySelectorAll('.ydisplay')) node.textContent = year || '—';
    for (const node of row.querySelectorAll('.genre-display-wrap')) {
      const label = document.createElement('span');
      label.className = genre ? 'gtag' : 'muted ghint';
      if (genre) {
        const text = document.createElement('span');
        text.className = 'gtag-label';
        text.textContent = genre;
        label.title = genre;
        label.append(text);
      } else label.textContent = '—';
      node.replaceChildren(label);
      if (node.closest('td[title]')) node.closest('td').title = genre;
    }
    const caption = row.querySelector('.tile-cap-sub');
    if (caption) caption.textContent = [artist, song.album].filter(Boolean).join(' · ');
    const input = row.querySelector('input[name="track"]');
    if (input) input.value = JSON.stringify({ ...JSON.parse(input.value), title, artist });
    const play = row.querySelector('.gen-track-play');
    if (play) {
      play.title = 'Play ' + title;
      play.setAttribute('aria-label', 'Play ' + title + ' on YouTube Music');
    }
    row.dataset.title = row.matches('tr') ? title.toLowerCase() : title;
    row.dataset.artist = row.matches('tr') ? artist.toLowerCase() : artist;
    row.dataset.genre = genre.toLowerCase();
    row.dataset.year = String(year || 0);
  }
  window.dispatchEvent(new CustomEvent('song-metadata-updated', { detail: { videoId, title, artist } }));
}

function songActions() {
  // Keep DOM nodes and the originating Alpine scope outside reactive data.
  let trigger = null, owner = null, request = 0, lookupStream = null, maintenanceObserver = null;
  let lookupTimer;
  const stopLookup = () => { clearTimeout(lookupTimer); lookupStream?.close(); lookupStream = null; };
  const metadataSaves = new Map();
  const metadataEdits = new Map();
  function markMetadataEdit(videoId, pending) {
    for (const button of document.querySelectorAll('[data-song]')) {
      if (JSON.parse(button.dataset.song).video_id !== videoId) continue;
      let notice = button.querySelector('.song-edit-status');
      if (!pending) { notice?.remove(); button.removeAttribute('aria-description'); continue; }
      if (notice) continue;
      notice = document.createElement('span');
      notice.className = 'song-edit-status';
      notice.setAttribute('role', 'status');
      notice.textContent = 'Edit not confirmed';
      button.setAttribute('aria-description', 'Metadata edit not confirmed. Open Edit Metadata to review.');
      button.append(notice);
    }
  }
  return {
    song: {}, context: {}, mode: 'create', name: '', query: '', selected: '',
    playlists: [], multipleIdentities: false, loading: false, busy: false, error: '', result: null,
    artworkFailed: false, readOnly: document.documentElement.dataset.storageCompacting === 'true',
    metadata: { title: '', artist: '', genre: '', year: '' }, initialMetadata: {}, originals: {},
    metadataLoaded: false, metadataPending: false, trackId: null, genreOptions: [], genres: [],
    lookupRunning: false, lookupStatus: '',
    reasons: [['vibe', 'Wrong vibe'], ['era', 'Wrong era'], ['mainstream', 'Too mainstream'],
              ['own_it', 'Already know it'], ['artist', 'Not this artist']],
    init() {
      maintenanceObserver = new MutationObserver(() => {
        this.readOnly = document.documentElement.dataset.storageCompacting === 'true';
      });
      maintenanceObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-storage-compacting'] });
    },
    show(detail) {
      if (this.$refs.dialog.open) return;
      const wasOpen = this.$refs.menu.matches(':popover-open');
      const same = trigger === detail.trigger;
      if (wasOpen) this.$refs.menu.hidePopover();
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
      if (wasOpen && same) return;
      request++;
      trigger = detail.trigger;
      owner = Alpine.$data(trigger);
      this.song = detail.song;
      this.metadataPending = metadataEdits.has(this.song.video_id);
      this.artworkFailed = false;
      this.context = detail.context;
      if (['create', 'add', 'metadata'].includes(detail.action)) {
        this.openDialog(detail.action);
        return;
      }
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
      const items = [...this.$refs.menu.querySelectorAll('[role="menuitem"]')].filter(el => !el.matches(':disabled') && el.getClientRects().length);
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
      tcPlay(this.context.play_url ||
        'https://music.youtube.com/watch?v=' + encodeURIComponent(this.song.video_id));
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
    destroy() { request++; stopLookup(); maintenanceObserver?.disconnect(); },
    async openDialog(mode) {
      if (document.documentElement.dataset.storageCompacting === 'true') return;
      stopLookup();
      this.closeMenu();
      this.mode = mode;
      this.name = this.song.title;
      this.query = ''; this.selected = ''; this.playlists = [];
      this.error = ''; this.result = null; this.busy = false;
      this.metadata = { title: this.song.title, artist: this.song.artist || '', genre: '', year: '' };
      this.originals = {}; this.metadataLoaded = false; this.trackId = null;
      this.genreOptions = []; this.genres = []; this.lookupStatus = ''; this.lookupRunning = false;
      this.loading = mode !== 'create';
      const current = ++request;
      this.$refs.dialog.showModal();
      const focus = mode === 'metadata' ? 'metadataTitle' : mode === 'create' ? 'name' : 'search';
      this.focusDialog(focus);
      if (mode === 'create') return;
      try {
        // Reopening the same song waits for its pending write before reading defaults.
        if (mode === 'metadata') await metadataSaves.get(this.song.video_id);
        if (current !== request) return;
        if (mode === 'metadata' && metadataEdits.has(this.song.video_id)) {
          Object.assign(this, metadataEdits.get(this.song.video_id).fields, {
            metadataLoaded: true, error: 'This edit was not confirmed. Review your changes before saving again.'
          });
          return;
        }
        const data = await this.json('/songs/' + (mode === 'metadata' ? 'metadata' : 'playlists') +
          '?video_id=' + encodeURIComponent(this.song.video_id));
        if (current !== request) return;
        if (mode === 'metadata') {
          if (data.metadata) {
            const { title, artist, genre, year, orig_title, orig_artist, track_id } = data.metadata;
            this.metadata = { title, artist: artist || '', genre, year: String(year) };
            this.originals = { title: orig_title, artist: orig_artist };
            this.trackId = track_id;
          }
          this.initialMetadata = { ...this.metadata };
          this.genreOptions = data.genre_options; this.genres = data.genres;
          this.metadataLoaded = true;
        } else {
          this.playlists = data.playlists;
          this.multipleIdentities = data.multiple_identities;
        }
      } catch (error) {
        if (current === request) this.error = error.message;
      } finally {
        if (current === request) {
          this.loading = false;
          this.focusDialog(focus);
        }
      }
    },
    async lookupGenres() {
      if (this.lookupRunning || !this.trackId) return;
      const current = request;
      this.lookupRunning = true; this.lookupStatus = 'Looking up genres…';
      const unavailable = () => {
        stopLookup();
        if (current === request) {
          this.lookupRunning = false;
          this.lookupStatus = 'Genre lookup did not finish. Your changes are kept.';
        }
      };
      const watch = () => { clearTimeout(lookupTimer); lookupTimer = setTimeout(unavailable, 20000); };
      try {
        const job = await this.json(`/track/${this.trackId}/genre-lookup`, {});
        if (current !== request) return;
        if (!Number.isSafeInteger(job.job_id)) throw new Error('Invalid genre lookup job');
        const stream = lookupStream = new EventSource(`/track/genre-lookup/events/${job.job_id}`);
        watch();
        stream.onmessage = async message => {
          if (current !== request) { stream.close(); return; }
          let event;
          try {
            event = JSON.parse(message.data);
            if (!event || typeof event !== 'object') throw new Error('Invalid genre lookup progress');
          }
          catch (error) { console.warn('Genre lookup progress could not be read', error); unavailable(); return; }
          watch();
          this.lookupStatus = genreLookupStatus(event);
          if (event.type !== 'end') return;
          stopLookup();
          if (event.error) { this.lookupRunning = false; return; }
          try {
            const data = await this.json('/songs/metadata?video_id=' + encodeURIComponent(this.song.video_id));
            if (current === request) {
              this.genreOptions = data.genre_options;
              this.lookupStatus = data.genre_options.length ? 'Choose a suggestion or enter your own genre.' : 'No genre suggestions found.';
            }
          } catch (error) {
            console.warn('Genre suggestions could not be refreshed', error);
            if (current === request) this.lookupStatus = 'Suggestions are not available yet. Your changes are kept.';
          } finally { if (current === request) this.lookupRunning = false; }
        };
        stream.onerror = unavailable;
      } catch (error) {
        console.warn('Genre lookup start was not confirmed', error);
        if (current === request) {
          this.lookupRunning = false;
          this.lookupStatus = 'Genre lookup was not confirmed. Your changes are kept.';
        }
      }
    },
    focusDialog(ref) {
      // Alpine reveals x-show elements on the next frame, after its reactive tick.
      const current = request;
      this.$nextTick(() => requestAnimationFrame(() => {
        if (request !== current || !this.$refs.dialog.open) return;
        const active = document.activeElement;
        // A delayed load must not move focus after the user starts editing a field.
        if (active?.matches('input') && active !== this.$refs[ref] &&
            this.$refs.dialog.contains(active) && active.getClientRects().length) return;
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
      stopLookup();
      this.$refs.dialog.close();
      trigger?.focus();
      if (this.result && (this.context.refresh_on_save || this.result.url === location.pathname)) location.replace(location.href);
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
      let validation;
      const messages = {
        playlist_missing: 'Playlist no longer exists',
        main_account_required: 'Choose a main account in Setup before continuing.',
        account_disconnected: 'The required account is not connected',
        song_playlist_name_required: 'Choose a song and give the playlist a name.',
        playlist_regular_required: 'Choose a regular playlist.',
        playlist_account_disconnected: "That playlist's account isn't connected.",
        song_add_rejected: "YouTube couldn't add this song. Please try another song or playlist."
      };
      try {
        const response = await fetch(url, body === undefined ? { signal: AbortSignal.timeout(20000) } : {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
          signal: AbortSignal.timeout(20000)
        });
        const data = await response.json();
        if (response.ok) return data;
        if ([400, 422].includes(response.status) && Object.hasOwn(messages, data?.code)) validation = messages[data.code];
        throw new Error('Song request returned ' + response.status);
      } catch (error) {
        console.debug('[Song actions] Request unavailable', url, error);
        throw new Error(validation || (body === undefined
          ? 'Song details are unavailable. Try opening this control again.'
          : 'This change was not confirmed. Review it before trying again.'));
      }
    },
    saveMetadata() {
      if (!this.metadataLoaded) return;
      const edit = { fields: {
        song: { ...this.song }, context: { ...this.context }, metadata: { ...this.metadata },
        initialMetadata: { ...this.initialMetadata }, originals: { ...this.originals },
        trackId: this.trackId, genreOptions: [...this.genreOptions], genres: [...this.genres]
      }};
      const changes = Object.fromEntries(Object.entries(edit.fields.metadata)
        .filter(([field, value]) => value !== edit.fields.initialMetadata[field]));
      const videoId = edit.fields.song.video_id;
      this.closeDialog();
      const save = async () => {
        try {
          await this.json('/songs/metadata', { song: edit.fields.song, ...changes });
          const data = await this.json('/songs/metadata?video_id=' + encodeURIComponent(videoId));
          if (data.metadata) applySongMetadata(videoId, data.metadata);
          metadataEdits.delete(videoId);
          if (this.song.video_id === videoId) this.metadataPending = false;
          markMetadataEdit(videoId, false);
        } catch (error) {
          // Keep this song's edits without taking over another song's form.
          // Its existing Edit Metadata control restores the draft on demand.
          console.debug('[Song actions] Metadata save unconfirmed', videoId, error);
          metadataEdits.set(videoId, edit);
          if (this.song.video_id === videoId) this.metadataPending = true;
          markMetadataEdit(videoId, true);
        } finally {
          metadataSaves.delete(videoId);
        }
      };
      metadataSaves.set(videoId, save());
    },
    async submit() {
      if (this.busy || this.loading || this.result) return;
      if (this.mode === 'metadata') { this.saveMetadata(); return; }
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
function toggleTrackLike(source, videoId, liked) {
  const request = new CustomEvent('track-like-requested', {
    detail: { videoId, liked }, cancelable: true,
  });
  // The global player handles its current song; every other song uses the library API.
  if (window.dispatchEvent(request)) {
    htmx.ajax('POST', '/track/like', {
      source, values: { video_id: videoId, on: liked ? '1' : '' }, swap: 'none',
    }).catch(() => {}); // the completion event rolls back the heart after an unconfirmed request
  }
}
