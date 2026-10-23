function genreDialog(trackId, initialChoice) {
  return {
    trackId, choice: initialChoice || '', overriding: false,
    genreList: [], genreSuggestions: [],
    lookupRunning: false, lookupFinished: false, lookupPct: 0, lookupStatus: '',
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
      let job;
      try {
        const response = await fetch(`/track/${this.trackId}/genre-lookup`, { method: 'POST' });
        if (!response.ok) throw new Error('start failed');
        job = (await response.json()).job_id;
      } catch (e) {
        this.lookupRunning = false; this.lookupStatus = 'Could not start lookup.'; return;
      }
      const stream = new EventSource(`/track/genre-lookup/events/${job}`);
      stream.onmessage = (message) => {
        const event = JSON.parse(message.data);
        if (event.type === 'track') {
          this.lookupPct = Math.round((event.i / event.n) * 100); this.lookupStatus = event.text;
        } else if (event.type === 'info' || event.type === 'done' || event.type === 'err') {
          this.lookupStatus = event.text;
          if (event.type === 'done') this.lookupPct = 100;
        } else if (event.type === 'end') {
          stream.close(); this.lookupRunning = false; this.lookupFinished = true;
          if (!event.error) {
            setTimeout(() => htmx.ajax('GET', `/track/${this.trackId}/genre-candidates`,
              { target: '#genre-candidates-modal', swap: 'innerHTML' }), 650);
          }
        }
      };
      stream.onerror = () => {
        stream.close(); this.lookupRunning = false; this.lookupFinished = true;
        this.lookupStatus = 'Lookup stream interrupted.';
      };
    },
  };
}
