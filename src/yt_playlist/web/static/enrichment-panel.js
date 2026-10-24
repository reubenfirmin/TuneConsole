function enrichPanel(pid, lastfmConfigured, activeJobId, activeSource, enrichBase, conflictCount) {
  // Enrichment: one background job runs the configured provider waterfall and streams over SSE.
  // Updates the Year/Genre cells live as each track resolves, drives a determinate progress bar, and
  // surfaces a conflict count (provider disagreements) that lights up the header's resolve icon.
  // `enrichBase` defaults to /playlist/<pid>; album pages pass /album/<browse>.
  return {
    pid: pid, enrichBase: enrichBase || ('/playlist/' + pid),
    lastfmConfigured: lastfmConfigured, running: false, finished: false, pct: 0, status: '', source: '',
    conflictCount: conflictCount || 0,
    _stream: null, _finishTimer: null, _disposed: false,
    destroy() {
      this._disposed = true;
      this._stream?.close(); clearTimeout(this._finishTimer);
    },
    openConflicts() {
      htmx.ajax('GET', `${this.enrichBase}/conflicts`, { target: '#conflicts-modal', swap: 'innerHTML' });
    },
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
        this.start('lastfm');                       // saved, run it now
      } catch (e) { this.keyBusy = false; this.keyErr = 'Could not save that key.'; }
    },
    // if the page was refreshed while a job is running, reattach to it and resume the progress UI
    rejoinIfActive() {
      if (activeJobId) {
        this.status = 'Reconnecting to enrichment in progress…';
        this.listen(activeJobId, activeSource);
      }
    },
    async start() {
      if (this.running) return;
      this.source = 'waterfall';
      this.running = true; this.finished = false; this.pct = 0;
      this.status = 'Starting enrichment…';
      let job;
      try {
        const r = await fetch(`${this.enrichBase}/enrich`, { method: 'POST' });
        job = (await r.json()).job_id;
      } catch (e) { this.status = 'Could not start.'; this.running = false; return; }
      this.listen(job, 'waterfall');
    },
    listen(job, source) {
      if (this._disposed) return;
      this.source = source; this.running = true; this.finished = false;
      clearTimeout(this._finishTimer);
      this._stream?.close();
      const es = this._stream = new EventSource(`/playlist/enrich/events/${job}`);
      let errs = 0;
      es.onmessage = (m) => {
        errs = 0;                          // a delivered event means we're reconnected: reset backoff
        const ev = JSON.parse(m.data);
        if (ev.type === 'track') {
          this.applyRow(ev);
          this.pct = Math.round((ev.i / ev.n) * 100);
          this.status = ev.text;
        } else if (ev.type === 'info') {
          this.status = ev.text;
        } else if (ev.type === 'done') {
          this.pct = 100; this.status = ev.text;
          if (typeof ev.conflicts === 'number') this.conflictCount = ev.conflicts;
        } else if (ev.type === 'err') {
          this.status = ev.text;
          if (ev.text && ev.text.includes('Last.fm API key')) {   // key missing/invalid: prompt for it
            this.lastfmConfigured = false; this.keyModal = true;
          }
        } else if (ev.type === 'end') {
          es.close(); this.running = false; this.finished = true;
          document.body.dispatchEvent(new CustomEvent('playlist-metadata-changed'));
          if (!ev.error) this._finishTimer = setTimeout(() => { this.finished = false; }, 4000);
        }
      };
      es.onerror = () => {
        // A transient drop (e.g. a proxy idle-timeout): let EventSource auto-reconnect. The server
        // replays events idempotently and sends 'end' once the job finishes, so a successful
        // background job no longer looks failed. Give up only after several consecutive failures.
        this.status = 'Reconnecting…';
        if (es.readyState === EventSource.CLOSED || ++errs >= 5) {
          es.close(); this.running = false;
          this.status = 'Progress is unavailable or expired. Reload to check the result.';
        }
      };
    },
    // HTMX owns fragment cleanup and initialization even when SSE supplies the HTML. Alpine's
    // observer handles the removed/inserted trees, as it does for an ordinary HTMX row swap.
    applyRow(ev) {
      if (!ev.row_html) return;
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(ev.video_id)}"]`);
      if (tr) htmx.swap(tr, ev.row_html, { swapStyle: 'outerHTML', settleDelay: 0 });
      document.body.dispatchEvent(new CustomEvent('playlist-metadata-changed'));
    },
  };
}

