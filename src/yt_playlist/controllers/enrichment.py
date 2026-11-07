"""Coordinate metadata selection and enrichment conflict state."""
from yt_playlist.core import enrichment_conflict_policy as policy
from yt_playlist.core.enrichment_contract import FIELD_COLUMN
from yt_playlist.core.metadata_contract import clock


class Enrichment:
    def __init__(self, repository, choices, files):
        self.repository, self.choices, self.files = repository, choices, files

    def apply_provider_fields(self, track_id, provider, fields, **options):
        return self.repository.apply_provider_fields(track_id, provider, fields, **options)

    def reuse_provider_fields(self, entry_id, provider, fields, **options):
        return self.repository.reuse_provider_fields(entry_id, provider, fields, **options)

    def replace_catalog_hints(self, track_id, fields):
        return self.repository.replace_catalog_hints(track_id, fields)

    def upsert_conflict(self, track_id, field, candidates, now=None, **options):
        return self.repository.upsert_conflict(track_id, field, candidates, now, **options)

    def retire_agreed_conflicts(self, track_id, evidence, now=None):
        return self.repository.retire_agreed_conflicts(track_id, evidence, now)

    def resolve_conflict(self, track_id, field, value, now=None):
        column, prepared = policy.selected_value(field, value)
        at = clock(now)
        observation = self.choices.choose(track_id, column, prepared, source='conflict_resolution', now=at)
        self.repository.mark_resolved(track_id, field, value, observation, at)

    def resolve_enrichment_candidate(self, track_id, field, value, now=None):
        spec = FIELD_COLUMN.get(field)
        if spec is None:
            return False
        at = clock(now)
        facts = self.repository.resolution_inputs(track_id, field, column=spec[0])
        if facts is None:
            return False
        evidence = facts['evidence']
        if 'reference' in evidence:
            identity, revision = evidence['reference']
            document = self.files.read('conflicts', (identity,), revision)
            candidates = policy.current_candidates(document, evidence['updated_at'], at)
        else:
            candidates = evidence['candidates']
        if not policy.offered_value(facts['current'], candidates, value):
            return False
        self.resolve_conflict(facts['entry_id'], field, value, now=at)
        return True
