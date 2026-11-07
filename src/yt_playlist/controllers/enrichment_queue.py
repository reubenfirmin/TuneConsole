"""Rank enrichment work and recheck each bounded batch before admission."""
import time

from yt_playlist.enrich import queue_policy
from yt_playlist.repos.enrichment_sql import pending_coverage


class EnrichmentQueue:
    def __init__(self, repository, state, settings, *, read_snapshot, memoized_read, read_only):
        self.repository = repository
        self.state = state
        self.settings = settings
        self.read_snapshot = read_snapshot
        self.memoized_read = memoized_read
        self.read_only = read_only

    @staticmethod
    def _track(row):
        # A job belongs to a recording; id is its exact catalog input, when present.
        fields = ('recording_id', 'source', 'id', 'identity_key', 'video_id', 'title', 'artist',
                  'last_enriched_at', 'thumbnail', 'mb_recording_id')
        return {'job_id': row['recording_id'], **{key: row[key] for key in fields
                if key != 'id' or row[key] is not None}}

    def priority_enrich_batch(self, keys, limit, stale_before, *, providers=None, now=None):
        now = time.time() if now is None else now
        selected = list(dict.fromkeys(keys))[:queue_policy.VISIBLE_TRACKS]
        rows = self.repository.priority_candidates(selected, limit, stale_before,
            providers=providers, now=now, eligibility=queue_policy.priority_eligibility(pending_coverage()))
        result = []
        for row in rows:
            track = self._track(row)
            if row['first_enriched_at'] is not None:
                jobs = self.state.enrichment_provider_jobs(track['recording_id'])
                if jobs:
                    track['provider_jobs'] = jobs
            result.append(track)
        return result

    def _select_enrich_batch(self, limit, offset=0, *, providers=None, now=None):
        if limit == 0 or providers == []:
            return []
        now = time.time() if now is None else now
        eligibility = queue_policy.primary_eligibility(pending_coverage())
        # A fully deferred or finished queue needs no listening/container calculation.
        if not self.repository.has_candidates(providers=providers, now=now, eligibility=eligibility):
            return []

        def priority_order():
            caught_up = float(self.settings.get_setting('enrich_caught_up_at') or 0)
            return self.repository.priority_order(caught_up=caught_up, ordering=queue_policy.PRIORITY_ORDER)

        priorities = self.memoized_read('enrichment-priority-order', priority_order)
        return self.repository.select_candidates(limit, offset, priorities=priorities,
            providers=providers, now=now, eligibility=eligibility)

    def _select_resweep_batch(self, limit, stale_before, *, providers, now):
        return self.repository.resweep_candidates(limit, stale_before, providers=providers,
            now=now, eligibility=queue_policy.resweep_eligibility(pending_coverage()))

    def _batch(self, limit, offset, *, providers, now, stale_before=None):
        def select(reader):
            if stale_before is None:
                return reader._select_enrich_batch(limit, offset, providers=providers, now=now)
            return reader._select_resweep_batch(limit, stale_before, providers=providers, now=now)

        if self.read_only:
            return [self._track(row) for row in select(self)]
        with self.read_snapshot() as snapshot:
            reader = snapshot.store.enrichment_queue
            configuration = reader.repository.provider_configuration(providers)
            candidates = select(reader)
        eligibility = (queue_policy.primary_eligibility(pending_coverage()) if stale_before is None
                       else queue_policy.resweep_eligibility(pending_coverage()))
        result = []
        # Usual batches contain 40 tracks. Diagnostic reads still release the lock regularly.
        for start in range(0, len(candidates), queue_policy.ADMISSION_BATCH):
            result.extend(self.repository.admit_candidates(
                candidates[start:start + queue_policy.ADMISSION_BATCH], configuration,
                providers=providers, now=now, stale_before=stale_before, eligibility=eligibility))
        return [self._track(row) for row in result]

    def next_enrich_batch(self, limit, offset=0, *, providers=None, now=None):
        return self._batch(limit, offset, providers=providers, now=time.time() if now is None else now)

    def resweep_batch(self, limit, stale_before, *, providers=None, now=None):
        return self._batch(limit, 0, stale_before=stale_before,
                           providers=providers, now=time.time() if now is None else now)

    def mark_recordings_enriched(self, recording_ids, now):
        return self.repository.mark_recordings_enriched(recording_ids, now)

    def requeue_enrichment(self, track_ids):
        return self.repository.requeue_enrichment(track_ids)
