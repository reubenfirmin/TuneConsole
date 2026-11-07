function enrichmentStatus(ev) {
  const count = key => Number.isSafeInteger(ev[key]) && ev[key] >= 0 ? ev[key] : 0;
  if (ev.type === 'err') return 'Enrichment did not finish. Updated details are kept.';
  if (ev.type === 'done') {
    if (ev.blocked) return 'No enrichment sources are ready. Your current details are kept.';
    let text = `Checked ${count('total')} song${count('total') === 1 ? '' : 's'}.`;
    if (count('remaining')) text += ` ${count('remaining')} song${count('remaining') === 1 ? ' remains' : 's remain'} for later.`;
    if (count('conflicts')) text += ` ${count('conflicts')} disagreement${count('conflicts') === 1 ? '' : 's'} to review.`;
    return text;
  }
  if (ev.type === 'track' && count('n')) return `${count('i')} of ${count('n')} songs checked.`;
  if (ev.warning || ev.deferred) return 'Some details remain for later. Checking the rest…';
  if (ev.waiting) return 'Waiting for enrichment…';
  return 'Checking song details…';
}

function enrichPanel(pid, activeJobId, activeSource, enrichBase, conflictCount) {
  // Enrichment: one background job runs the configured provider waterfall and streams over SSE.
  // Updates the Year/Genre cells live as each track resolves, drives a determinate progress bar, and
  // surfaces a conflict count (provider disagreements) that lights up the header's resolve icon.
  // `enrichBase` defaults to /playlist/<pid>; album pages pass /album/<browse>.
  return {
    pid: pid, enrichBase: enrichBase || ('/playlist/' + pid),
    running: false, finished: false, pct: 0, status: '', source: '',
    conflictCount: conflictCount || 0,
    _stream: null, _finishTimer: null, _idleTimer: null, _startController: null,
    _disposed: false, _owner: 0, _sawDone: false,
    destroy() {
      this._disposed = true; ++this._owner;
      this._startController?.abort();
      this._stream?.close(); this._stream = null;
      clearTimeout(this._finishTimer); clearTimeout(this._idleTimer);
    },
    openConflicts() {
      htmx.ajax('GET', `${this.enrichBase}/conflicts`, { target: '#conflicts-modal', swap: 'innerHTML' });
    },
    // if the page was refreshed while a job is running, reattach to it and resume the progress UI
    rejoinIfActive() {
      if (activeJobId) {
        this.status = 'Checking song details…';
        this.listen(activeJobId, activeSource, ++this._owner);
      }
    },
    async start() {
      if (this.running || this._disposed) return;
      const owner = ++this._owner;
      clearTimeout(this._finishTimer);
      this.source = 'waterfall';
      this.running = true; this.finished = false; this.pct = 0;
      this.status = 'Starting enrichment…';
      let job;
      const controller = this._startController = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 20000);
      try {
        const r = await fetch(`${this.enrichBase}/enrich`, { method: 'POST', signal: controller.signal });
        if (!r.ok) throw new Error(`Enrichment start HTTP ${r.status}`);
        job = (await r.json()).job_id;
        if (!Number.isSafeInteger(job) || job < 1) throw new Error('Invalid enrichment job');
      } catch (e) {
        console.warn('Enrichment start did not return a job', e);
        if (!this._disposed && this._owner === owner) this.complete(false);
        return;
      } finally {
        clearTimeout(timeout);
        if (this._startController === controller) this._startController = null;
      }
      if (!this._disposed && this._owner === owner) this.listen(job, 'waterfall', owner);
    },
    complete(success) {
      this._stream?.close(); this._stream = null;
      clearTimeout(this._idleTimer); clearTimeout(this._finishTimer);
      this.running = false; this.finished = true;
      if (!success) this.status = 'Your current details are kept. You can check enrichment again.';
      document.body.dispatchEvent(new CustomEvent('playlist-metadata-changed'));
      this._finishTimer = setTimeout(() => { if (!this._disposed) this.finished = false; }, 4000);
    },
    listen(job, source, owner = ++this._owner) {
      if (this._disposed || this._owner !== owner) return;
      this.source = source; this.running = true; this.finished = false;
      this._sawDone = false;
      clearTimeout(this._finishTimer); clearTimeout(this._idleTimer);
      this._stream?.close();
      const es = this._stream = new EventSource(`/playlist/enrich/events/${job}`);
      let errs = 0;
      const current = () => !this._disposed && this._owner === owner && this._stream === es;
      const armIdle = () => {
        clearTimeout(this._idleTimer);
        this._idleTimer = setTimeout(() => {
          if (!current()) return;
          console.warn('Enrichment progress stopped arriving');
          this.complete(false);
        }, 20000);
      };
      armIdle();
      es.onmessage = (m) => {
        if (!current()) return;
        errs = 0;                          // a delivered event means we're reconnected: reset backoff
        let ev;
        try {
          ev = JSON.parse(m.data);
          if (!ev || typeof ev !== 'object' || !['track', 'activity', 'info', 'done', 'err', 'end'].includes(ev.type)) {
            throw new Error('Invalid enrichment progress');
          }
        } catch (e) {
          console.warn('Enrichment progress could not be read', e);
          this.complete(false); return;
        }
        armIdle();
        if (ev.type === 'track') {
          this.applyRow(ev);
          if (Number.isSafeInteger(ev.i) && Number.isSafeInteger(ev.n) && ev.n > 0) {
            this.pct = Math.max(0, Math.min(100, Math.round((ev.i / ev.n) * 100)));
          }
          this.status = enrichmentStatus(ev);
        } else if (ev.type === 'info' || ev.type === 'activity') {
          this.status = enrichmentStatus(ev);
        } else if (ev.type === 'done') {
          this._sawDone = true;
          this.pct = 100; this.status = enrichmentStatus(ev);
          if (Number.isSafeInteger(ev.conflicts) && ev.conflicts >= 0) this.conflictCount = ev.conflicts;
        } else if (ev.type === 'err') {
          this.status = enrichmentStatus(ev);
        } else if (ev.type === 'end') {
          this.complete(!ev.error && this._sawDone);
        }
      };
      es.onerror = () => {
        if (!current()) return;
        // A transient drop (e.g. a proxy idle-timeout): let EventSource auto-reconnect. The server
        // replays events idempotently and sends 'end' once the job finishes, so a successful
        // background job no longer looks failed. Give up only after several consecutive failures.
        this.status = 'Your current details are kept.';
        if (es.readyState === EventSource.CLOSED || ++errs >= 5) {
          console.warn('Enrichment progress could not be resumed');
          this.complete(false);
        }
      };
    },
    // HTMX owns fragment cleanup and initialization even when SSE supplies the HTML. Alpine's
    // observer handles the removed/inserted trees, as it does for an ordinary HTMX row swap.
    applyRow(ev) {
      if (typeof ev.row_html !== 'string' || !ev.row_html || typeof ev.video_id !== 'string') return;
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(ev.video_id)}"]`);
      if (tr) htmx.swap(tr, ev.row_html, { swapStyle: 'outerHTML', settleDelay: 0 });
      document.body.dispatchEvent(new CustomEvent('playlist-metadata-changed'));
    },
  };
}

