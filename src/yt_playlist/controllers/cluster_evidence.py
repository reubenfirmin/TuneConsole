"""Admit library relationships and search groups for cluster explanations."""
from yt_playlist.rec.cluster_explain import MAX_EVIDENCE_PLAYLIST


class ClusterEvidence:
    def __init__(self, repository):
        self.repository = repository

    def connection(self, key, path_keys, *, max_playlist=MAX_EVIDENCE_PLAYLIST):
        return self.repository.connection(
            key, path_keys, max_playlist=max_playlist, exclude_quarantined=True, generated_group='Generated')

    def seed_matches(self, query, *, limit=8):
        return self.repository.seed_matches(query, limit=limit, generated_group='Generated')
