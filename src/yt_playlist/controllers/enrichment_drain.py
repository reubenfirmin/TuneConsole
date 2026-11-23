"""One queue-processing command, independent of thread and application context."""
from yt_playlist.providers.waterfall import REGISTRY

RADIO_RETRY_SECONDS = 3600


def positive_integer(value, default):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def resweep_days(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 30.0


class EnrichmentDrain:
    def __init__(self, settings, queue, reports, state, *, now, configuration,
                 run_batch, retry_artwork, refresh, logger):
        self.settings, self.queue, self.reports, self.state = settings, queue, reports, state
        self.now, self.configuration, self.run_batch = now, configuration, run_batch
        self.retry_artwork, self.refresh, self.logger = retry_artwork, refresh, logger
        self.had_work = False

    def drain(self, *, limit=None, priority=(), registry=None, should_stop=lambda: False,
              activity=lambda **values: None):
        if should_stop():
            return 0, set()
        self.state.expire_provider_alternatives(self.now())
        config = self.configuration(registry or REGISTRY)
        self.state.reconcile_provider_configuration(config)
        providers = [name for name, value in config.items() if value['status'] == 'available']
        batch, scope, artwork = [], 'radio', 0
        if providers:
            limit = limit or positive_integer(self.settings.get_setting('enrich_batch_size', '40'), 40)
            days = resweep_days(self.settings.get_setting('enrich_resweep_days', '30'))
            stale_before = self.now() - days * 86400
            if priority:
                radio_before = max(stale_before, self.now() - RADIO_RETRY_SECONDS)
                batch = self.queue.priority_enrich_batch(priority, min(limit, 24), radio_before,
                    providers=providers, now=self.now())
        if not batch:
            scope = 'queue'
            artwork = self.retry_artwork(should_stop=should_stop,
                on_progress=lambda event: activity(warning=event['text']))
            if providers:
                batch = self.queue.next_enrich_batch(limit, providers=providers, now=self.now())
        if batch:
            self.had_work = True
        else:
            pending = None
            if self.had_work:
                pending = self.reports.has_pending_tracks()
                if not pending:
                    self.queue.save_caught_up_at(self.now())
                    self.had_work = False
            if providers:
                batch = self.queue.resweep_batch(limit, stale_before, providers=providers, now=self.now())
            scope = 'refresh'
            if not batch:
                if pending is None:
                    pending = self.reports.has_pending_tracks()
                activity(clear=not pending, state='waiting' if providers else 'blocked',
                    text=('Waiting for usable metadata or pending provider retries; other eligible work continues.'
                          if providers else 'No enrichment providers are available. Check Setup.'))
                return artwork, set()
        if scope == 'queue':
            for track in batch:
                if track.get('last_enriched_at') is not None:
                    track['provider_jobs'] = self.state.enrichment_provider_jobs(track['recording_id'])
        completed = self.run_batch(batch, scope, should_stop, activity)
        keys = {track['identity_key'] for track in batch if track['job_id'] in completed}
        if completed:
            try:
                self.refresh(keys)
            except Exception:
                self.logger.warning('recommendation refresh after enrich batch failed', exc_info=True)
        return len(completed) + artwork, keys


class EnrichmentBatch:
    """Publish completed recordings as progress arrives, preserving partial batches."""
    def __init__(self, queue, config, waterfall, sink_for, *, now, stopped):
        self.queue, self.config, self.waterfall, self.sink_for = queue, config, waterfall, sink_for
        self.now, self.stopped = now, stopped

    def run(self, batch, scope, should_stop, activity):
        completed, batch_keys = set(), {track['job_id'] for track in batch}

        def mark(keys):
            self.queue.mark_recordings_enriched(keys, self.now())

        def progress(event):
            if event.get('warning'):
                activity(warning=event['text'])
                return
            key = event.get('job_id', event.get('track_id'))
            if event.get('type') == 'track' and event.get('fully_attempted') and key in batch_keys and key not in completed:
                mark([key])
                completed.add(key)
            activity(state='blocked' if event.get('blocked') else 'waiting' if event.get('waiting') else 'running',
                     text=event.get('text', ''))

        activity(state='running', scope=scope, text='Starting lookups…')
        try:
            attempted = self.waterfall(batch, self.config(), on_progress=progress,
                should_stop=should_stop, sink_for=self.sink_for, now_fn=self.now, reuse_completed=True)
            if not self.stopped():
                attempted = (set() if should_stop() else batch_keys) if attempted is None else set(attempted) & batch_keys
                mark(attempted - completed)
                completed.update(attempted)
            return completed
        except Exception:
            activity(state='error', text='A lookup failed. Retrying shortly; see Network for request details.')
            raise
