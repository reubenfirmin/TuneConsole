"""Coordinate retained contexts, explicit SQL roots and orphan-file collection."""
import time

from yt_playlist.controllers.group_reference_context import resolve
from yt_playlist.core import group_evidence, recommendation_references
from yt_playlist.core.prepared_references import FIELDS, PREPARED
from yt_playlist.core.recommendation_outputs import CollectionProgress


class GroupEvidence:
    def __init__(self, repository, ledger, admission, files, output_guard, *, enabled, recommendations):
        self.repository, self.ledger, self.admission = repository, ledger, admission
        self.files, self.output_guard = files, output_guard
        self.enabled, self.recommendations = enabled, recommendations
        self._orphan_after = ''

    def replace_prepared(self, surface, data, at, *, historical=False, resolved_contexts=None):
        if not self.enabled or surface not in PREPARED and not surface.startswith('model_pair:'):
            return
        at = time.time() if at is None else at
        data = data if isinstance(data, dict) else {}
        kind = 'comparison' if surface.startswith('model_pair:') else 'prepared'
        if kind == 'comparison' and isinstance(data.get('data'), dict):
            data = data['data']
        generations = recommendation_references.prepared_generations(surface, data) if self.recommendations else None
        operations = []
        for field in FIELDS:
            identity = 'proposal:' + surface + ':' + field
            if historical and self.repository.has_owner(kind, identity):
                continue
            value = data.get(field) if field == 'context' else None
            if surface == 'adaptive_group_preview' and field == 'context' and data.get('generation'):
                value = {'group_generation': data['generation']}
            owner, unknown, references = None, None, []
            if value:
                try:
                    if resolved_contexts is None:
                        value = resolve(value, self.ledger(), recommendations=self.recommendations)
                    elif field in resolved_contexts:
                        value = resolved_contexts[field]
                    else:
                        raise ValueError('Historical prepared context is no longer retained')
                except ValueError:
                    if not historical:
                        raise
                    raw = value.get('artifact', identity) if isinstance(value, dict) else identity
                    unknown = (raw, 'missing_context', at)
                else:
                    generation, versions = group_evidence.references(value)
                    if self.recommendations:
                        references = recommendation_references.references(value)
                    if generation is not None or versions:
                        owner = group_evidence.prepare(kind, identity, value, at)
            operations.append((identity, owner, unknown, references))
        return self.repository.replace_prepared(kind, surface, operations, generations, historical=historical)

    def retain(self, kind, identity, value, at, *, strict, recommendations):
        owner = group_evidence.prepare(kind, identity, value, at)
        return self.repository.retain(owner, strict=strict, recommendations=recommendations)

    def release(self, kind, identity):
        return self.repository.release(kind, identity)

    def retire(self, kind, identities):
        return self.repository.retire(kind, identities, mixes=kind == 'offer')

    def collect_mix_dictionary(self):
        return self.repository.collect_mix_dictionary()

    def collect_unpublished_mixes(self, limit=128):
        with self.admission():
            identities = self.repository.mix_menu_ids(self._orphan_after, limit)
            ledger = self.ledger()
            missing = [identity for identity in identities if not ledger.contains('opportunity', identity)]
            self.repository.remove_unpublished_mixes(missing, limit)
            self._orphan_after = identities[-1] if len(identities) == limit else ''
            return len(missing)

    def output_collector(self, *, groups=False):
        return self.files.collector(groups=groups)

    def collect_output_files(self, collector):
        with self.output_guard(), collector.candidates() as names:
            if names is None:
                return CollectionProgress(pending=True, blocked=True)
            owned = self.repository.protected_output_files(names, collector.owners)
            return collector.remove_unowned(owned)

    def has_owner(self, kind, identity):
        return self.repository.has_owner(kind, identity)

    def identities(self, kind, after, limit):
        return self.repository.identities(kind, after, limit)

    def next_prepared(self, after, max_bytes):
        return self.repository.next_prepared(after, max_bytes)

    def matches_prepared(self, surface, payload, at):
        return self.repository.matches_prepared(surface, payload, at)

    def roots_complete(self):
        return self.repository.roots_complete()

    def complete_roots(self):
        return self.repository.complete_roots()

    def retain_unknown(self, kind, identity, raw, reason, at):
        return self.repository.retain_unknown(kind, identity, raw, reason, at)
