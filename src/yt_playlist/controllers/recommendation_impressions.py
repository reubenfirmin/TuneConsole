"""Ordered diagnostic publication and bounded recommendation-event maintenance."""
import json

from yt_playlist.core import served_facts

RETENTION_BATCH_SIZE = 256


class RecommendationImpressions:
    def __init__(self, repository, files, guard):
        self.repository, self.files, self.guard = repository, files, guard

    def record(self, rows, *, versions):
        if not rows:
            return
        with self.guard():
            if not self.repository.served_facts_active():
                return self.repository.insert_served_impressions(rows, versions=versions)
            traces = [json.loads(row[8]) for row in rows]
            facts = [served_facts.extract(trace) for trace in traces]
            with self.files.retained():
                # Reserve monotonic IDs in SQL, then publish files, then their owners.
                # Interrupted attempts leave expirable files and gaps in the sequence.
                reserved = self.repository.reserve_impression_ids(rows)
                self.files.publish_many([(identity, rows[index][9], traces[index]) for identity, index in reserved])
                prepared = [(identity, (*rows[index][:8], served_facts.compact_provenance(rows[index][8]), rows[index][9]),
                             facts[index]) for identity, index in reserved]
                self.repository.insert_reserved_impressions(prepared, versions=versions)

    def facts(self, impression_id):
        with self.files.retained():
            row = self.repository.served_recommendation_facts(impression_id)
            if row is None:
                return None
            result = dict(row)
            fact = result.pop('facts')
            result['provenance'] = json.loads(result['provenance'])
            if fact is not None:
                trace = self.files.diagnostic({**result, **fact})
                result['diagnostic_status'] = 'expired' if trace is None else 'available'
                result['provenance'] = trace if trace is not None else {
                    **served_facts.reporting_trace(fact), **result['provenance']}
            return result

    def maintain(self, now, *, before):
        with self.guard():
            if not self.repository.served_facts_available():
                self.repository._prune_impression_batch(before)
                self.repository._prune_response_batch(before)
                return
            with self.files.collector() as admitted:
                if not admitted:
                    return
                self.repository.expire_diagnostics(now - served_facts.RETAIN_SECONDS, limit=RETENTION_BATCH_SIZE)
                self.repository._prune_impression_batch(before)
                self.repository._prune_response_batch(before)
                retired = self.repository.diagnostic_retirements(limit=RETENTION_BATCH_SIZE)
                removed = self.files.delete_many(retired)
                if removed:
                    self.repository.acknowledge_diagnostic_retirements(removed)
                candidates = self.files.orphan_candidates(now - served_facts.RETAIN_SECONDS, limit=RETENTION_BATCH_SIZE)
                if candidates:
                    owned = self.repository.existing_impressions([identity for identity, _ in candidates])
                    self.files.delete_orphans([(identity, expected) for identity, expected in candidates if identity not in owned])
