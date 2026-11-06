"""Read retained enrichment evidence and compose review and progress displays."""
import time

from yt_playlist.core.enrichment_contract import FIELD_COLUMN
from yt_playlist.core.metadata_contract import ALTERNATIVE_SECONDS
from yt_playlist.enrich import reporting

_FIELDS = {field: spec[0] for field, spec in FIELD_COLUMN.items()}


class EnrichmentReports:
    def __init__(self, repository, read_document, *, now_fn=time.time):
        self.repository = repository
        self.read_document = read_document
        self.now_fn = now_fn

    def _candidates(self, evidence):
        if evidence is None:
            return []
        if 'candidates' in evidence:
            return evidence['candidates']
        identity, revision = evidence['reference']
        document = self.read_document('conflicts', (identity,), revision)
        return reporting.current_candidates(document, evidence['updated_at'], self.now_fn())

    def genre_provenance(self, track_id):
        facts = self.repository.genre_provenance_facts(track_id)
        if facts is None:
            return None
        candidates = facts['candidates']
        if facts['runtime']:
            candidates, now = [], self.now_fn()
            for reference in facts['provider_references']:
                if reference['at'] <= now - ALTERNATIVE_SECONDS:
                    continue
                data = self.read_document('alternatives',
                    (reference['recording_id'], reference['provider_id']), float(reference['at']), missing=True)
                if data:
                    candidates.append({'provider': reference['provider'], 'value': data.get('fields', {}).get('genre') or ''})
        return reporting.genre_provenance(facts['track'], candidates, self._candidates(facts['conflict']))

    def _conflicts(self, rows):
        return [{**{key: value for key, value in row.items() if key != 'evidence'},
                 'candidates': self._candidates(row['evidence'])} for row in rows]

    def _review(self, rows):
        result = []
        for row in rows:
            facts = {key: row[key] for key in ('current', 'selected_source', 'findings', 'overridden')}
            conflict = {key: value for key, value in row.items() if key not in facts and key != 'evidence'}
            conflict['candidates'] = self._candidates(row['evidence'])
            result.append(reporting.review_conflict(conflict, **facts))
        return result

    def unresolved_enrichment_conflicts(self, limit=20, offset=0):
        return self._review(self.repository.unresolved_enrichment_conflicts(limit, offset, fields=_FIELDS))

    def enrichment_conflict_detail(self, track_id, field):
        row = self.repository.enrichment_conflict_detail(track_id, field, fields=_FIELDS)
        return self._review([row])[0] if row is not None else None

    def unresolved_conflicts_for_playlist(self, playlist_id):
        return self._conflicts(self.repository.unresolved_conflicts_for_playlist(playlist_id))

    def unresolved_conflicts_for_album(self, browse_id):
        return self._conflicts(self.repository.unresolved_conflicts_for_album(browse_id))

    def processed_timeline(self, buckets=40):
        return reporting.processed_timeline(self.repository.processed_timestamps(), buckets)

    def enrichment_activity_stats(self, now):
        return self.repository.activity_stats(now - 900)

    def conflict_count_for_playlist(self, playlist_id):
        return self.repository.conflict_count_for_playlist(playlist_id)

    def conflict_count_for_album(self, browse_id):
        return self.repository.conflict_count_for_album(browse_id)

    def outstanding_conflicts(self):
        return self.repository.outstanding_conflicts()

    def coverage_stats(self, include_discovery=False):
        return self.repository.coverage_stats(include_discovery)

    def queue_remaining(self):
        return self.repository.queue_remaining()

    def queue_stats(self):
        return self.repository.queue_stats()

    def enriched_tracks(self, limit=40, offset=0):
        return self.repository.enriched_tracks(limit, offset)
