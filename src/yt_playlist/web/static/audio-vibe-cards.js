// Playback cards survive navigation; only explicit clicks become judgments.
const KEY = 'tuneconsole.audio-vibes.cards.1';
const FEEDBACK_LOCK = 'tuneconsole-audio-vibe-feedback';
const CARDS_LOCK = 'tuneconsole-audio-vibe-cards';

export class AudioVibeCards {
  constructor({root, request, read, write, compatible, taxonomy, renderInspector}) {
    Object.assign(this, {root, request, read, write, compatible, taxonomy, renderInspector});
    this.cards = new Map();
    this.restored = new Set();
    this.loads = new Map();
    this.edits = new Map();
    this.active = null;
    this.saving = false;
    this.retryAt = 0;
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
      return this.write(KEY, records);
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
        if (!this.restored.has(record.id)) throw new Error('Saved card state is unavailable');
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
    if (!record && !await this.update(id, current => current ? null : {
      dismissed: false, draft: answer?.vibes ?? null,
      previous_answer: !state.feedback && !!state.previous_feedback,
      playback: state.playback || state.samples[0],
      sample_ids: state.feedback?.sample_ids ?? [], dirty: false})) return;
    // The sampler may have inserted a blank placeholder before this response.
    // Restore human choices without overwriting a click or posting a new answer.
    // A previous occurrence's sample IDs must never attach to this occurrence.
    if (!placeholder && answer) await this.update(id, current =>
      current && !current.dirty && current.draft == null && !this.edits.get(id)?.length
        ? {draft: answer.vibes, sample_ids: state.feedback?.sample_ids ?? [],
           previous_answer: !state.feedback && !!state.previous_feedback} : null);
    if (this.stopped || this.records().find(row => row.id === id)?.dismissed) return;
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
    card.find('prediction').textContent = guess
      ? 'Prediction: ' + current.choices[guess].attribute_label
      : current.collection_status?.phase === 'unavailable' ? current.collection_status.message
      : `${current.samples.length} of 3 audio checks complete`;
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
    const available = records.filter(row => !row.dismissed && this.cards.has(row.id));
    if (!available.some(row => row.id === this.active)) this.active = available[0]?.id ?? null;
    for (const [id, card] of this.cards) {
      const record = records.find(row => row.id === id);
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
        ? (Date.now() < this.retryAt ? 'Saved here · retrying sync…' : 'Saving…')
        : record?.previous_answer ? 'Previous answer' : record?.draft != null ? 'Saved' : 'Click to label';
    }
    this.root.hidden = !available.length;
    this.root.dataset.stacked = String(available.length > 1);
    this.find('deck').hidden = available.length < 2;
    this.find('count').textContent = `${available.findIndex(row => row.id === this.active) + 1} of ${available.length} songs`;
    this.find('next').textContent = `Next song · ${Math.max(0, available.length - 1)} more`;
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
      try {
        await this.update(id, record => {
          if (!record) return null;
          const selected = record.draft || [];
          const draft = remove ? selected.filter(value => value !== key) : [...new Set([...selected, key])];
          return {draft, sample_ids: edit.sample_ids, previous_answer: false,
            dirty: true, mutation: edit.mutation};
        });
      } finally {
        this.edits.set(id, (this.edits.get(id) || []).filter(pending => pending !== edit));
        this.render(); this.flush();
      }
    } else if (event.target.closest('[data-vibe-dismiss]') && card) {
      if (await this.update(id, {dismissed: true})) { this.render(); this.focusCard(); }
    } else if (event.target.closest('[data-vibe-next]')) {
      const available = this.records().filter(row => !row.dismissed && this.cards.has(row.id));
      this.active = available[(available.findIndex(row => row.id === this.active) + 1) % available.length]?.id;
      this.render(); this.focusCard();
    } else if (event.target.closest('[data-vibe-inspect]') && card) {
      const {state} = card;
      this.find('inspect-title').textContent = (state.playback || state.samples[0]).title;
      this.find('evidence').textContent = state.prediction?.evidence.join('. ')
        || `${state.samples.length} of 3 audio checks complete. Your labels can be saved now.`;
      this.find('alternatives').textContent = state.prediction ? 'Target fits: ' + state.prediction.ranking.map(row =>
        `${state.choices[row.vibe].attribute_label} ${row.score.toFixed(2)}`).join(' · ') : '';
      this.renderInspector(this.find('attributes'), state.samples);
      this.find('diagnostics').showModal();
    } else if (event.target.closest('[data-vibe-inspect-close]')) this.find('diagnostics').close();
  }

  focusCard() {
    const heading = this.cards.get(this.active)?.find('track');
    if (heading) { heading.tabIndex = -1; heading.focus({preventScroll: true}); }
  }

  async flush() {
    if (this.stopped || this.saving || Date.now() < this.retryAt || !navigator.locks
        || !this.records().some(row => row.dirty)) return;
    this.saving = true;
    try {
      // App tabs share one durable intent queue and serialize HTTP writes, so
      // a slow older response cannot overwrite a newer click in another tab.
      await navigator.locks.request(FEEDBACK_LOCK, async () => {
        let record;
        while (!this.stopped && (record = this.records().find(row => row.dirty))) {
          try {
            if (record.playback) await this.request('/audio-vibes/runs', {
              run_id: record.id, ...Object.fromEntries(['video_id', 'title', 'artist'].map(key => [key, record.playback[key]]))});
            await this.request('/audio-vibes/feedback', {run_id: record.id,
              sample_ids: record.sample_ids, taxonomy_version: this.taxonomy, vibes: record.draft});
            const saved = await this.update(record.id, latest =>
              latest?.mutation === record.mutation ? {dirty: false} : null);
            if (!saved) throw new Error('Could not persist feedback sync state');
          } catch (error) {
            this.retryAt = Date.now() + 5000;
            break;
          }
        }
      });
    } finally { this.saving = false; this.render(); }
  }

  interacting() {
    const selection = window.getSelection();
    return !this.root.hidden && (this.root.matches(':hover') || this.root.contains(document.activeElement)
      || this.find('diagnostics').open || this.records().some(row => row.dirty)
      || !!(selection && !selection.isCollapsed && this.root.contains(selection.anchorNode)));
  }

  tick() {
    this.flush();
    if (!this.restoring && this.records().some(row => !row.dismissed && !this.restored.has(row.id)
        && Date.now() >= (this.loads.get(row.id)?.retryAt || 0))) {
      this.restoring = true;
      this.restore().finally(() => { this.restoring = false; });
    }
  }
}
