// Playback cards survive navigation; only explicit clicks become judgments.
const KEY = 'tuneconsole.audio-vibes.cards.1';
const FEEDBACK_LOCK = 'tuneconsole-audio-vibe-feedback';
const CARDS_LOCK = 'tuneconsole-audio-vibe-cards';
const RUN = 'tuneconsole.audio-vibes.run.1';
// More than the extension's 24 recent occurrences. Keep the current occurrence
// even when it predates this window, so heartbeats cannot reopen its card.
const DISMISSED_LIMIT = 64;

export class AudioVibeCards {
  constructor({root, request, read, write, compatible, taxonomy, renderInspector, diagnose = () => {}}) {
    Object.assign(this, {root, request, read, write, compatible, taxonomy, renderInspector, diagnose});
    this.cards = new Map();
    this.restored = new Set();
    this.loads = new Map();
    this.edits = new Map();
    this.writes = new Map();
    this.storageFailed = new Set();
    this.recoveryFailed = new Set();
    this.active = null;
    this.saving = false;
    this.stopped = false;
    this.find = name => root.querySelector(`[data-vibe-${name}]`);
    root.addEventListener('click', event => this.clicked(event));
    window.addEventListener('storage', event => {
      if (event.key === KEY) { this.restore(); this.render(); }
    });
    this.restoring = false;
    this.tick();
  }

  records() { return this.read(KEY, []); }

  compact(records) {
    const acknowledged = row => row.dismissed && !row.dirty && row.draft != null && !row.previous_answer;
    const recent = new Set(records.filter(acknowledged).slice(-DISMISSED_LIMIT).map(row => row.id));
    const current = this.read(RUN)?.id;
    return records.filter(row => !acknowledged(row) || row.id === current || recent.has(row.id)).map(row =>
      // Keep the last acknowledged labels for a different tab's memory-only
      // add/remove operation. Measurements and predictions stay on the server.
      acknowledged(row) ? {id: row.id, dismissed: true, draft: row.draft, tombstone: true} : row);
  }

  async compactHistory() {
    if (!navigator.locks) return;
    await navigator.locks.request(CARDS_LOCK, () => {
      const records = this.records(), compacted = this.compact(records);
      if (JSON.stringify(records) !== JSON.stringify(compacted)) this.write(KEY, compacted);
    });
    this.render();
  }

  available(record) {
    // A dismissal in another tab cannot hide this tab's memory-only intent or
    // the action that can save it after browser storage recovers.
    return this.cards.has(record.id) && (!record.dismissed || this.edits.get(record.id)?.length);
  }

  async update(id, change) {
    if (!navigator.locks) return false;
    // Read and mutate the shared index under one lock. Keep this separate from
    // the HTTP lock so clicks remain durable while an older request is in flight.
    return navigator.locks.request(CARDS_LOCK, () => {
      const records = this.records();
      const index = records.findIndex(row => row.id === id);
      const patch = typeof change === 'function' ? change(records[index]) : change;
      if (!patch) return true;
      if (index < 0) records.push({id, ...patch});
      else records[index] = {...records[index], ...patch};
      return this.write(KEY, this.compact(records));
    });
  }

  async restore() {
    // Only the small card index and pending clicks live in localStorage. Full
    // measurements are already durable on the server, not duplicated per card.
    for (const record of this.records()) {
      if (this.stopped) return;
      const load = this.loads.get(record.id) || {pending: false, failures: 0, retryAt: 0};
      if (record.dismissed || this.restored.has(record.id) || load.pending || Date.now() < load.retryAt) continue;
      load.pending = true;
      this.loads.set(record.id, load);
      try {
        if (record.playback && !this.cards.has(record.id)) {
          await this.accept({run_id: record.id, playback: record.playback,
            samples: [], prediction: null, taxonomy_version: this.taxonomy,
            choices: JSON.parse(this.root.dataset.choices)}, {placeholder: true});
        }
        const query = record.playback ? '?video_id=' + encodeURIComponent(record.playback.video_id) : '';
        const state = await this.request('/audio-vibes/runs/' + encodeURIComponent(record.id) + query);
        state.playback ||= record.playback;
        await this.accept(state);
        const latest = this.records().find(row => row.id === record.id);
        if (latest && !latest.dismissed && !this.restored.has(record.id))
          throw new Error('Saved card state is unavailable');
      } catch (error) {
        // Keeping the human form available does not establish that its saved
        // checks loaded. Retry failures independently, with a bounded backoff.
        load.failures += 1;
        load.retryAt = Date.now() + Math.min(30000, 2000 * 2 ** Math.min(load.failures - 1, 4));
      } finally { load.pending = false; }
    }
  }

  async accept(state, {placeholder = false} = {}) {
    if (this.stopped || !this.compatible(state) || !(state.playback || state.samples[0])) return;
    const id = state.run_id;
    const record = this.records().find(row => row.id === id);
    const answer = state.feedback || state.previous_feedback;
    if (record?.dismissed) return;
    // Only observing playback admits a new occurrence. A GET or sample receipt
    // that finishes after dismissal/compaction cannot recreate a retired card.
    if (!record && !placeholder) return;
    if (!record && !await this.update(id, current => current ? null : {
      dismissed: false, draft: answer?.vibes ?? null,
      previous_answer: !state.feedback && !!state.previous_feedback,
      playback: state.playback || state.samples[0],
      sample_ids: state.feedback?.sample_ids ?? [], dirty: false})) {
      this.diagnose('card_blocked', {reason: navigator.locks ? 'storage_unavailable' : 'web_locks_unavailable'});
      return;
    }
    // The sampler may have inserted a blank placeholder before this response.
    // Restore human choices without overwriting a click or posting a new answer.
    // A previous occurrence's sample IDs must never attach to this occurrence.
    if (!placeholder && answer) await this.update(id, current =>
      current && !current.dirty && current.draft == null && !this.edits.get(id)?.length
        ? {draft: answer.vibes, sample_ids: state.feedback?.sample_ids ?? [],
           previous_answer: !state.feedback && !!state.previous_feedback} : null);
    const latest = this.records().find(row => row.id === id);
    if (this.stopped || !latest || latest.dismissed) return;
    if (!placeholder) { this.restored.add(id); this.loads.delete(id); }
    if (!this.cards.has(id)) {
      const node = this.find('card-template').content.firstElementChild.cloneNode(true);
      node.dataset.runId = id;
      node.hidden = true;
      const find = name => node.querySelector(`[data-vibe-${name}]`);
      const sample = state.playback || state.samples[0];
      find('track').textContent = sample.title || 'Untitled song';
      find('track').title = sample.title;
      find('artist').textContent = sample.artist;
      const group = node.querySelector('.audio-vibe-choices');
      group.replaceChildren();
      for (const [key, choice] of Object.entries(state.choices)) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'audio-vibe-choice';
        button.dataset.vibeChoice = key; button.title = choice.description;
        const label = document.createElement('strong'); label.textContent = choice.attribute_label;
        button.append(label); group.append(button);
      }
      this.cards.set(id, {node, state, find});
      this.find('cards').append(node);
    }
    const card = this.cards.get(id);
    // A delayed GET cannot erase completed checks. Each saved excerpt is immutable.
    if (state.samples.length >= card.state.samples.length) card.state = state;
    else if ('collection_status' in state) card.state.collection_status = state.collection_status;
    const current = card.state;
    const guess = current.prediction && (current.prediction.vibe || current.prediction.ranking[0]?.vibe);
    // Compatible installed extensions and retained views can contain older
    // diagnostic messages. Never use their free-form text in the human card.
    card.find('prediction').textContent = guess
      ? 'Prediction: ' + current.choices[guess].attribute_label
      : current.collection_status?.update ? 'Update to TuneConsole extension 0.1.24 or later to resume audio checks.'
      : current.collection_status?.phase === 'unavailable' ? 'Audio checks are paused. You can still label this song.'
      : `${current.samples.length} of 3 audio checks complete`;
    card.find('extension-update').hidden = !current.collection_status?.update;
    const checks = card.find('checks');
    checks.replaceChildren();
    for (const [point, label] of [['thirty', '30 seconds'], ['middle', 'Halfway'], ['two_thirds', 'Two-thirds']]) {
      const complete = current.samples.some(sample => sample.point === point);
      const light = document.createElement('span');
      light.className = 'audio-vibe-check'; light.dataset.complete = String(complete);
      light.setAttribute('role', 'img'); light.setAttribute('aria-label', `${label}: ${complete ? 'complete' : 'pending'}`);
      checks.append(light);
    }
    this.render();
  }

  render() {
    const records = this.records();
    const byId = new Map(records.map(record => [record.id, record]));
    for (const [id, card] of this.cards) {
      const record = byId.get(id);
      if (record) card.record = record;
      if (!this.edits.get(id)?.length && (!record || record.dismissed)) {
        if (this.inspecting === id) {
          this.find('diagnostics').close();
          this.find('attributes').replaceChildren();
          delete this.find('attributes').dataset.samples;
          for (const name of ['inspect-title', 'evidence', 'alternatives']) this.find(name).textContent = '';
          this.inspecting = null;
        }
        card.node.remove();
        this.cards.delete(id);
        this.restored.delete(id);
        this.loads.delete(id);
        this.storageFailed.delete(id);
        this.recoveryFailed.delete(id);
      } else if (!record && card.record) {
        // Another tab may retire acknowledged history while this tab still
        // holds an unsaved click. Keep its recovery form, not retired history.
        records.push(card.record);
        byId.set(id, card.record);
      }
    }
    for (const id of this.loads.keys()) {
      if (!byId.has(id) || byId.get(id).dismissed) this.loads.delete(id);
    }
    const available = records.filter(row => this.available(row));
    if (!available.some(row => row.id === this.active)) this.active = available[0]?.id ?? null;
    for (const [id, card] of this.cards) {
      const record = byId.get(id);
      const edits = this.edits.get(id) || [];
      const selected = new Set(record?.draft || []);
      for (const edit of edits) {
        if (edit.remove) selected.delete(edit.key);
        else selected.add(edit.key);
      }
      card.node.hidden = id !== this.active;
      for (const button of card.node.querySelectorAll('[data-vibe-choice]')) {
        button.disabled = !this.restored.has(id);
        button.setAttribute('aria-pressed', String(selected.has(button.dataset.vibeChoice)));
      }
      card.find('feedback-status').textContent = !this.restored.has(id) ? 'Loading saved answer…' : record?.dirty || edits.length
        ? ((this.recoveryFailed.has(id) || this.storageFailed.has(id)) && edits.length ? 'Not saved yet · keep this page open.'
          : record?.sync_error?.rejected ? 'Saved here · needs attention'
          : Date.now() < (record?.retry_at || 0) ? 'Saved here · retrying sync…' : 'Saving…')
        : record?.previous_answer ? 'Previous answer' : record?.draft != null ? 'Saved' : 'Click to label';
      const unsaved = this.storageFailed.has(id) && edits.length;
      card.find('retry').hidden = !unsaved && !record?.sync_error;
      card.find('retry').textContent = unsaved ? 'Retry saving' : 'Retry sync';
      card.find('sync-detail').hidden = !record?.sync_error?.rejected;
      card.find('sync-detail').textContent = record?.sync_error?.rejected
        ? 'Your labels are kept here and have not synced yet.' : '';
      card.find('dismiss').disabled = !!edits.length;
    }
    const rejected = records.filter(row => row.dismissed && row.dirty && row.sync_error?.rejected);
    this.find('pending').hidden = !rejected.length;
    this.find('pending').textContent = `${rejected.length} saved ${rejected.length === 1 ? 'answer needs' : 'answers need'} attention`;
    this.root.hidden = !available.length && !rejected.length;
    this.root.dataset.stacked = String(available.length > 1);
    this.find('deck').hidden = available.length < 2;
    this.find('count').textContent = `${available.findIndex(row => row.id === this.active) + 1} of ${available.length} songs`;
    this.find('next').textContent = `Next song · ${Math.max(0, available.length - 1)} more`;
    this.diagnose('cards', {run_id: this.active, card_count: available.length,
      card_visible: !this.root.hidden, can_label: !!this.active && this.restored.has(this.active),
      samples: this.cards.get(this.active)?.state.samples.length ?? 0});
  }

  async clicked(event) {
    const node = event.target.closest('[data-vibe-card]');
    const card = node && this.cards.get(node.dataset.runId);
    const id = node?.dataset.runId;
    const choice = event.target.closest('[data-vibe-choice]');
    if (choice && card && !choice.disabled) {
      const key = choice.dataset.vibeChoice;
      // Preserve the explicit add/remove the user saw, then apply it to the
      // latest shared draft. Two tabs adding the same label do not cancel it.
      const remove = choice.getAttribute('aria-pressed') === 'true';
      // Capture the checks visible at the click, before waiting for another
      // tab's storage lock. Later analysis cannot become earlier evidence.
      const edit = {key, remove, mutation: crypto.randomUUID(),
        sample_ids: card.state.samples.map(sample => sample.id)};
      const edits = this.edits.get(id) || [];
      edits.push(edit);
      this.edits.set(id, edits);
      this.render();
      // Persist the intent before sending; rapid clicks, navigation, dismissal
      // and a failed request cannot silently erase a label or its removal.
      this.persistEdits(id);
    } else if (event.target.closest('[data-vibe-retry]') && card) {
      if (this.edits.get(id)?.length) this.persistEdits(id);
      else {
        await this.update(id, record => record?.sync_error
          ? {sync_error: null, retry_at: 0, retry_attempts: 0} : null);
        this.render(); this.flush();
      }
    } else if (event.target.closest('[data-vibe-pending]')) {
      const record = this.records().find(row => row.dismissed && row.dirty && row.sync_error?.rejected);
      if (record && await this.update(record.id, {dismissed: false})) {
        await this.restore(); this.active = record.id; this.render(); this.focusCard();
      }
    } else if (event.target.closest('[data-vibe-dismiss]') && card) {
      if (this.edits.get(id)?.length) return;
      if (await this.update(id, {dismissed: true})) { this.render(); this.focusCard(); }
    } else if (event.target.closest('[data-vibe-next]')) {
      const available = this.records().filter(row => this.available(row));
      this.active = available[(available.findIndex(row => row.id === this.active) + 1) % available.length]?.id;
      this.render(); this.focusCard();
    } else if (event.target.closest('[data-vibe-inspect]') && card) {
      const {state} = card;
      this.inspecting = id;
      this.find('inspect-title').textContent = (state.playback || state.samples[0]).title;
      this.find('evidence').textContent = state.prediction?.evidence.join('. ')
        || `${state.samples.length} of 3 audio checks complete. Your labels can be saved now.`;
      this.find('alternatives').textContent = state.prediction ? 'Target fits: ' + state.prediction.ranking.map(row =>
        `${state.choices[row.vibe].attribute_label} ${row.score.toFixed(2)}`).join(' · ') : '';
      this.renderInspector(this.find('attributes'), state.samples);
      this.find('diagnostics').showModal();
    } else if (event.target.closest('[data-vibe-inspect-close]')) this.find('diagnostics').close();
  }

  persistEdits(id) {
    if (this.writes.has(id)) return this.writes.get(id);
    const write = async () => {
      let recovering = false;
      try {
        while (!this.stopped && this.edits.get(id)?.length) {
          if (!this.records().some(row => row.id === id)) {
            // An arbitrarily old tab can retain an unsaved operation beyond
            // the tombstone window. Recover the latest acknowledged answer
            // after older HTTP writes finish, then apply the original click.
            recovering = true;
            await navigator.locks.request(FEEDBACK_LOCK, async () => {
              if (this.records().some(row => row.id === id)) return;
              const card = this.cards.get(id);
              const playback = card.state.playback || card.state.samples[0];
              const state = await this.request('/audio-vibes/runs/' + encodeURIComponent(id)
                + '?video_id=' + encodeURIComponent(playback.video_id));
              if (!this.compatible(state)) throw new Error('Saved card state is unavailable');
              await this.update(id, current => current ? null : {
                dismissed: false, draft: state.feedback?.vibes ?? null,
                sample_ids: state.feedback?.sample_ids ?? [], playback, dirty: false});
            });
            recovering = false;
            this.recoveryFailed.delete(id);
          }
          const edit = this.edits.get(id)[0];
          let applied = false;
          const saved = await this.update(id, record => {
            if (!record) return null;
            applied = true;
            const selected = record.draft || [];
            const draft = edit.remove ? selected.filter(value => value !== edit.key)
              : [...new Set([...selected, edit.key])];
            const state = this.cards.get(id).state;
            return {draft, sample_ids: edit.sample_ids, previous_answer: false, dismissed: false, tombstone: false,
              playback: record.playback || state.playback || state.samples[0],
              dirty: true, mutation: edit.mutation, sync_error: null, retry_at: 0, retry_attempts: 0};
          });
          if (!saved || !applied) { this.storageFailed.add(id); return; }
          this.storageFailed.delete(id);
          this.recoveryFailed.delete(id);
          const remaining = this.edits.get(id).filter(pending => pending !== edit);
          if (remaining.length) this.edits.set(id, remaining);
          else this.edits.delete(id);
        }
      } catch (error) {
        this.storageFailed.add(id);
        if (recovering) this.recoveryFailed.add(id);
      }
      finally { this.writes.delete(id); this.render(); this.flush(); }
    };
    const pending = Promise.resolve().then(write);
    this.writes.set(id, pending);
    return pending;
  }

  focusCard() {
    const heading = this.cards.get(this.active)?.find('track');
    if (heading) { heading.tabIndex = -1; heading.focus({preventScroll: true}); }
  }

  async flush() {
    const eligible = row => row.dirty && !row.sync_error?.rejected && Date.now() >= (row.retry_at || 0);
    if (this.stopped || this.saving || !navigator.locks || !this.records().some(eligible)) return;
    this.saving = true;
    try {
      // App tabs share one durable intent queue and serialize HTTP writes, so
      // a slow older response cannot overwrite a newer click in another tab.
      await navigator.locks.request(FEEDBACK_LOCK, async () => {
        let record;
        const attempted = new Set();
        while (!this.stopped && (record = this.records().find(row => !attempted.has(row.id) && eligible(row)))) {
          attempted.add(record.id);
          try {
            if (record.playback) await this.request('/audio-vibes/runs', {
              run_id: record.id, ...Object.fromEntries(['video_id', 'title', 'artist'].map(key => [key, record.playback[key]]))});
            await this.request('/audio-vibes/feedback', {run_id: record.id,
              sample_ids: record.sample_ids, taxonomy_version: this.taxonomy, vibes: record.draft});
            const saved = await this.update(record.id, latest =>
              latest?.mutation === record.mutation
                ? {dirty: false, sync_error: null, retry_at: 0, retry_attempts: 0} : null);
            if (!saved) throw new Error('Could not persist feedback sync state');
          } catch (error) {
            const status = error.status || 0;
            const rejected = status >= 400 && status < 500 && ![408, 429].includes(status);
            await this.update(record.id, latest => latest?.mutation === record.mutation ? {
              sync_error: {status, rejected}, retry_attempts: (latest.retry_attempts || 0) + 1,
              retry_at: Date.now() + Math.min(30000, 5000 * 2 ** Math.min(latest.retry_attempts || 0, 3)),
            } : null);
          }
        }
      });
    } finally { this.saving = false; this.render(); }
  }

  interacting() {
    const selection = window.getSelection();
    return this.edits.size > 0 || !this.root.hidden && (this.root.matches(':hover') || this.root.contains(document.activeElement)
      || this.find('diagnostics').open || this.records().some(row => row.dirty)
      || !!(selection && !selection.isCollapsed && this.root.contains(selection.anchorNode)));
  }

  tick() {
    this.compactHistory();
    this.flush();
    if (!this.restoring && this.records().some(row => !row.dismissed && !this.restored.has(row.id)
        && Date.now() >= (this.loads.get(row.id)?.retryAt || 0))) {
      this.restoring = true;
      this.restore().finally(() => { this.restoring = false; });
    }
  }
}
