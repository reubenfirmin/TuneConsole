// Scheduling follows normal playback. Missed points use the next available excerpt.
export function samplingPoints(duration) {
  const points = [{key: 'thirty', seconds: 30, label: '30 seconds'}];
  if (Number.isFinite(duration) && duration > 0) points.push(
    {key: 'middle', seconds: duration / 2, label: 'Halfway'},
    {key: 'two_thirds', seconds: duration * 2 / 3, label: 'Two-thirds'});
  return points.sort((a, b) => a.seconds - b.seconds);
}

export class AudioVibeSampler {
  constructor({session, playback, cached, save, changed, result, clock = Date.now}) {
    Object.assign(this, {session, playback, save, changed, result, clock});
    this.run = cached || null;
    this.active = null;
    this.lastSaved = 0;
  }

  persist() {
    if (!this.run) return;
    this.run.updated_at = this.clock();
    this.lastSaved = this.clock();
    this.save(this.run);
    this.changed(this.run);
  }

  tick({collect = true} = {}) {
    const raw = this.playback();
    if (this.active && !this.session.current) {
      const interrupted = this.active.run;
      interrupted.points[this.active.point.key] = 'interrupted';
      interrupted.retry_after ||= {};
      const attempts = interrupted.attempts[this.active.point.key] || 1;
      interrupted.retry_after[this.active.point.key] = this.clock() + Math.min(30000, 5000 * 2 ** Math.min(attempts - 1, 3));
      this.active = null;
      this.persist();
    }
    // Keep a paused song's prediction visible. Stale or disconnected status
    // never supplies a clock for starting another sample.
    if (!raw?.video_id || !Number.isFinite(raw.position)) return;
    const playing = this.session.validPlaying();
    const position = playing?.position ?? raw.position;
    const replay = this.run?.video_id === raw.video_id && position < 5
      && this.run.last_position > Math.max(30, (raw.duration || Infinity) * .8);
    if (!this.run || this.run.video_id !== raw.video_id || replay
        || this.clock() - this.run.updated_at > 86400000) {
      if (!playing) return;
      // A complete excerpt still belongs to the old song and can finish in
      // the background. Only an unfinished recording is cancelled on a skip.
      if (this.session.current && !this.session.current.analysing) {
        this.session.stop('The playing song changed.');
        this.active = null;
      }
      this.run = {id: crypto.randomUUID(), video_id: raw.video_id,
        title: raw.title || '', artist: raw.artist || '', duration: raw.duration || null,
        last_position: position, points: {}, attempts: {}, retry_after: {}, dismissed: false};
      this.persist();
    }
    this.run.last_position = position;
    if (Number.isFinite(raw.duration) && raw.duration > 0) this.run.duration = raw.duration;
    if (this.clock() - this.lastSaved >= 2500) this.persist();
    // Playback identity and human feedback keep advancing even when uploads
    // are backed up. Only reserving another audio excerpt needs queue capacity.
    if (!collect || !playing || !this.session.port) return;
    const points = samplingPoints(this.run.duration);
    this.run.attempts ||= {};
    this.run.retry_after ||= {};
    for (const point of points) {
      const previous = this.run.points[point.key];
      if (['saved', 'too_short'].includes(previous)) continue;
      if (['saving', 'waiting', 'capturing', 'analysing'].includes(previous)) return;
      if (this.run.duration && Math.max(point.seconds, position) + 10 > this.run.duration + .1) {
        this.run.points[point.key] = 'too_short'; this.persist(); continue;
      }
      if (position < point.seconds && !this.session.schedule) continue;
      // Retry the earlier failed excerpt before reserving the source for a
      // later sampling point. A future reservation would block that recovery.
      if (this.clock() < (this.run.retry_after[point.key] || 0)) return;
      if (this.active || this.session.current) continue;
      this.active = {run: this.run, point, duration: this.run.duration,
        reason: previous || position > point.seconds + 5 ? 'catch_up' : 'scheduled'};
      this.run.attempts[point.key] = (this.run.attempts[point.key] || 0) + 1;
      this.run.points[point.key] = 'capturing';
      this.persist();
      if (this.session.schedule) this.session.schedule(point.seconds);
      else this.session.start();
      // A synchronous disconnect can fail inside start(). Count and report it
      // just like an asynchronous failure, rather than retrying every tick.
      if (!this.session.current && this.active) {
        this.run.points[point.key] = 'interrupted';
        this.run.retry_after[point.key] = this.clock() + Math.min(30000,
          5000 * 2 ** Math.min(this.run.attempts[point.key] - 1, 3));
        this.active = null;
        this.persist();
      }
      return;
    }
  }

  completed(value) {
    const capture = this.active;
    this.active = null;
    if (!capture || value.observed_playback.video_id !== capture.run.video_id) return;
    capture.run.points[capture.point.key] = 'saving';
    this.persist();
    this.result(sampleMetadata(value, capture));
  }
}

export function sampleMetadata(value, {run, point, duration, reason = 'scheduled'}) {
  const prediction = value.prediction;
  const position = value.song_position_at_start ?? value.observed_position_at_start;
  return {id: crypto.randomUUID(), run_id: run.id, point: point.key,
    capture_reason: position > point.seconds + 5 ? 'catch_up' : reason,
    target_seconds: point.seconds, position_seconds: position,
    source_position_seconds: value.source_position_at_start ?? null,
    duration_seconds: duration, video_id: value.observed_playback.video_id,
    title: value.observed_playback.title || '', artist: value.observed_playback.artist || '',
    metadata_verified: value.observed_playback.metadata_verified === true,
    source_identity_verified: false, recommendation_adoption: false, raw_audio_retained: false,
    analysis_location: value.analysis_location, audio_transferred_to_app: value.audio_transferred_to_app,
    analysed_seconds: prediction.analysed_seconds, tags: prediction.scores,
    audio_attributes: value.audio_attributes || null,
    ...(prediction.embedding ? {embedding: prediction.embedding} : {}),
    traits: Object.fromEntries(Object.entries(prediction.moods.traits).map(([key, trait]) =>
      [key, {score: trait.score, per_patch: trait.per_patch}])),
    model: prediction.model, model_version: prediction.model_version,
    mood_versions: Object.fromEntries(Object.entries(prediction.moods.models).map(([key, model]) => [key, model.version])),
    artifact_hashes: Object.fromEntries((prediction.bundle.files || []).map(file => [file.path, file.sha256]))};
}
