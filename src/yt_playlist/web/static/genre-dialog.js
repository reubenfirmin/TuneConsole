function genreLookupStatus(event) {
  // These streams can include provider warnings as ordinary progress events.
  // Never treat their free-form text as display copy, including successful ends.
  if (event.type === 'err' || (event.type === 'end' && event.error))
    return 'Genre lookup did not finish. Your changes are kept.';
  if (event.warning || event.deferred)
    return 'Some suggestions are not available yet. Checking the rest…';
  if (event.type === 'done') return 'Genre lookup finished.';
  return 'Looking up genres…';
}

const genreSaves = new Map();

function genreDialog(trackId, initialChoice) {
  let stream, idleTimer, controller, request = 0, alive = true;
  const stop = () => { clearTimeout(idleTimer); stream?.close(); stream = null; };
  return {
    trackId, open: true, choice: initialChoice || '', overriding: false,
    saving: false, saveStatus: '',
    genreList: [], genreSuggestions: [],
    lookupRunning: false, lookupFinished: false, lookupPct: 0, lookupStatus: '',
    destroy() { alive = false; request++; stop(); controller?.abort(); },
    close() { this.open = false; this.destroy(); },
    async apply() {
      if (this.saving || !this.choice.trim() || !alive) return;
      if (genreSaves.has(trackId)) {
        this.saving = true;
        this.saveStatus = 'An earlier genre change is still being checked. Your choice is kept.';
        try { await genreSaves.get(trackId); } catch (_) { /* The earlier picker owns its outcome. */ }
        if (alive) {
          this.saving = false;
          this.saveStatus = 'Review your choice before applying it.';
        }
        return;
      }
      this.saving = true; this.saveStatus = '';
      const pending = fetch(`/track/${trackId}/genre-candidates`, {
        method: 'POST', body: new URLSearchParams({genre: this.choice.trim()}),
        signal: AbortSignal.timeout(20000),
      });
      genreSaves.set(trackId, pending);
      try {
        const response = await pending;
        if (response.status !== 204 || response.headers.get('HX-Refresh') !== 'true')
          throw new Error('Genre save was not confirmed');
        if (!alive || !this.$el.isConnected) return;
        this.close();
        window.navigation.navigate(location.href, {history: 'replace', info: {refresh: true}});
      } catch (error) {
        console.warn('Genre save was not confirmed', error);
        if (alive && this.$el.isConnected)
          this.saveStatus = 'This genre change was not confirmed. Review your choice before trying again.';
      } finally {
        genreSaves.delete(trackId);
        if (alive) this.saving = false;
      }
    },
    openOverride() {
      this.overriding = true;
      if (!this.genreList.length)
        this.genreList = Array.from(document.querySelectorAll('#genrelist option')).map(o => o.value);
      this.genreSuggestions = this.genreList.slice(0, 12);
      this.$nextTick(() => { this.$refs.override.focus(); this.$refs.override.select(); });
    },
    filterGenres() {
      const q = (this.choice || '').trim().toLowerCase();
      this.genreSuggestions = (q ? this.genreList.filter(g => g.toLowerCase().includes(q))
                                 : this.genreList).slice(0, 12);
    },
    async startLookup() {
      if (this.lookupRunning) return;
      this.lookupRunning = true; this.lookupFinished = false; this.lookupPct = 0;
      this.lookupStatus = 'Starting metadata lookup…';
      const current = ++request;
      controller = new AbortController();
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(20000)]);
      let job;
      try {
        const response = await fetch(`/track/${this.trackId}/genre-lookup`, { method: 'POST', signal });
        if (!response.ok) throw new Error('start failed');
        job = (await response.json()).job_id;
        if (!Number.isSafeInteger(job)) throw new Error('Invalid genre lookup job');
      } catch (e) {
        console.warn('Genre lookup start was not confirmed', e);
        if (alive && current === request) {
          this.lookupRunning = false; this.lookupFinished = true;
          this.lookupStatus = 'Genre lookup was not confirmed. Your changes are kept.';
        }
        return;
      }
      if (!alive || current !== request) return;
      const unavailable = () => {
        stop();
        if (alive && current === request) {
          this.lookupRunning = false; this.lookupFinished = true;
          this.lookupStatus = 'Genre lookup did not finish. Your changes are kept.';
        }
      };
      const watch = () => { clearTimeout(idleTimer); idleTimer = setTimeout(unavailable, 20000); };
      stream = new EventSource(`/track/genre-lookup/events/${job}`);
      watch();
      stream.onmessage = async (message) => {
        if (!alive || current !== request) { stop(); return; }
        let event;
        try {
          event = JSON.parse(message.data);
          if (!event || typeof event !== 'object') throw new Error('Invalid genre lookup progress');
        }
        catch (error) { console.warn('Genre lookup progress could not be read', error); unavailable(); return; }
        watch();
        this.lookupStatus = genreLookupStatus(event);
        if (event.type === 'track') {
          if (event.n > 0) this.lookupPct = Math.min(100, Math.round((event.i / event.n) * 100));
        } else if (event.type === 'done') {
          this.lookupPct = 100;
        } else if (event.type === 'end') {
          stop();
          if (!event.error) {
            try {
              const response = await fetch(`/track/${this.trackId}/genre-candidates`, {
                signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20000)])
              });
              if (!response.ok) throw new Error('Genre suggestions returned ' + response.status);
              const html = new DOMParser().parseFromString(await response.text(), 'text/html');
              const suggestions = html.querySelector('.genre-suggestions');
              if (!suggestions) throw new Error('Genre suggestions are missing');
              if (!alive || current !== request) return;
              // Keep the form, chosen genre, custom input and lookup owner mounted.
              this.$refs.suggestions.replaceChildren(...suggestions.childNodes);
              this.lookupStatus = 'Choose a suggestion or keep your own genre.';
            } catch (error) {
              console.warn('Genre suggestions could not be refreshed', error);
              if (alive && current === request)
                this.lookupStatus = 'Suggestions are not available yet. Your changes are kept.';
            }
          }
          if (alive && current === request) { this.lookupRunning = false; this.lookupFinished = true; }
        }
      };
      stream.onerror = unavailable;
    },
  };
}
