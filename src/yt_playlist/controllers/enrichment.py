"""Coordinate metadata selection and enrichment conflict state."""
from yt_playlist.core import enrichment_conflict_policy as policy
from yt_playlist.core import enrichment_selection_policy as selections
from yt_playlist.core.enrichment_contract import FIELD_COLUMN
from yt_playlist.core.metadata_contract import clock
from yt_playlist.core.metadata_contract import ALTERNATIVE_SECONDS
from yt_playlist.core.metadata_values import scope


class Enrichment:
    def __init__(self, repository, choices, files):
        self.repository, self.choices, self.files = repository, choices, files

    def apply_provider_fields(self, track_id, provider, fields, *, providers=(), delivery_id=None, now=None):
        at = clock(now)
        facts = self.repository.provider_inputs(track_id, provider)
        observations = {}
        if facts['layout'] == 'runtime' and delivery_id is not None:
            selections.validate_capture(facts, provider, fields, delivery_id)
            if facts['provider_at'] is not None and facts['provider_at'] >= delivery_id.at:
                return {}
        captured = selections.canonical_fields(fields)
        if facts['layout'] == 'evidence':
            if delivery_id is not None:
                observations = self.choices.result_evidence(facts['entry_id'], provider, delivery_id, fields)
            elif captured:
                _, observations = self.choices.capture_fields(facts['entry_id'], provider, captured, purpose='provider-fields',
                    body={'format': 'catalog-provider-fields-1', 'fields': captured}, now=at)
        self._select_provider_fields(facts, provider, captured, providers, observations, at)
        return observations

    def _select_provider_fields(self, facts, provider, fields, providers, observations, at):
        for field, value in fields.items():
            if value in (None, ''):
                continue
            if facts['layout'] == 'legacy':
                self.repository.store_finding(facts['entry_id'], facts['recording_id'], field, provider, value, at)
            if not selections.provider_wins(field, value, provider, facts['sources'].get(field), facts['genre'], providers):
                continue
            self.choices.select(facts['entry_id'], field, value, provider, now=at, observation_id=observations.get(field))
            if facts['layout'] != 'runtime':
                self.repository.retire_entry_hints(facts['layout'], facts['entry_id'], field)

    def reuse_provider_fields(self, entry_id, provider, fields, *, providers=(), now=None, select=True):
        at = clock(now)
        facts = self.repository.provider_inputs(entry_id, provider)
        if facts['layout'] == 'runtime':
            data = {}
            reference = self.repository.alternative_references(facts['recording_id'], [provider]).get(provider)
            if reference and reference['at'] > at - ALTERNATIVE_SECONDS:
                data = self.files.read('alternatives', (facts['recording_id'], reference['provider_id']),
                                       float(reference['at']), missing=True) or {}
            scoped = selections.scoped_fields(facts, data)
            fields = {field: value for field, value in fields.items() if field in scoped and scoped[field] == value}
        elif facts['attempts']:
            observations = {}
            rows = self.repository.reuse_inputs(facts['recording_id'], provider, fields)
            for concept, value in fields.items():
                field = 'mb_year' if concept == 'year' else concept
                if not selections.canonical_fields({concept: value}):
                    continue
                row = rows.get(concept)
                reuse = selections.reusable_observation(facts, field, value, row)
                if reuse:
                    observations[field] = (self.choices.reuse_recording_observation(facts['entry_id'], row['id'], now=at)
                                           if reuse == 'transfer' else row['id'])
            if select:
                eligible = {field: value for field, value in selections.canonical_fields(fields).items() if field in observations}
                self._select_provider_fields(facts, provider, eligible, providers, observations, at)
            return observations
        if select:
            self.apply_provider_fields(facts['entry_id'], provider, fields, providers=providers, now=at)
        return None

    def replace_catalog_hints(self, track_id, fields):
        facts = self.repository.hint_inputs(track_id, fields)
        if facts is None:
            return
        for field, value in fields.items():
            if field not in facts['fields'] or value is None:
                continue
            previous = facts['fields'][field]
            if selections.replaces_hint(facts['layout'], previous['source'], previous['current'], previous['hints']):
                self.choices.select(facts['entry_id'], field, value, 'catalog_hint_replacement')
                self.repository.retire_hints(facts['layout'], field,
                    [(hint['entry_id'], hint['source']) for hint in previous['hints']])

    def upsert_conflict(self, track_id, field, candidates, now=None, *, refreshed_providers=None):
        field, prepared = policy.prepare_candidates(field, candidates)
        at = clock(now)
        facts = self.repository.conflict_inputs(track_id, field)
        if facts is None:
            raise ValueError('Unknown metadata conflict input')
        if facts['layout'] == 'evidence':
            for candidate in prepared:
                if candidate.get('observation_id') is None:
                    _, observations = self.choices.capture_fields(facts['entry_id'], candidate['provider'],
                        {field: candidate['value']}, purpose='conflict-offer:' + field,
                        body={'format': 'metadata-conflict-offer-1', 'field': field, 'value': candidate['value']}, now=at)
                    candidate['observation_id'] = observations[field]
        with self.repository.conflict_transaction():
            facts = self.repository.conflict_inputs(track_id, field)
            previous = next((row for row in facts['conflicts'] if row['entry_id'] == facts['entry_id']),
                            facts['conflicts'][0] if facts['conflicts'] else None)
            old = self._candidates(previous, at) if previous else []
            current = prepared
            if facts['layout'] == 'runtime':
                references = self.repository.alternative_references(facts['recording_id'],
                    list({candidate['provider'] for candidate in prepared}))
                current = []
                for candidate in prepared:
                    reference = references.get(candidate['provider'])
                    supplied = {}
                    if reference and reference['at'] > at - ALTERNATIVE_SECONDS:
                        supplied = self.files.read('alternatives', (facts['recording_id'], reference['provider_id']),
                                                   float(reference['at']), missing=True) or {}
                    captured_at = candidate.get('at', supplied.get('at', at))
                    if captured_at > at - ALTERNATIVE_SECONDS:
                        current.append({'provider': candidate['provider'], 'value': candidate['value'], 'at': captured_at})
            current = [*current, *policy.retain_candidates(old, prepared, refreshed_providers)]
            status = policy.refreshed_status(previous['status'] if previous else 0, old, current)
            identity = previous['id'] if previous else None
            revision = None
            if facts['layout'] == 'runtime':
                identity = identity if identity is not None else self.repository.reserve_conflict_identity()
                revision = previous['revision'] + 1 if previous else 1
                self.files.publish('conflicts', (identity,), revision, {'candidates': current},
                                   current_revision=previous['revision'] if previous else None)
            self.repository.store_conflict(facts, field, current, status, at, scope=scope(field),
                                           identity=identity, revision=revision)

    def retire_agreed_conflicts(self, track_id, evidence, now=None):
        at = clock(now)
        with self.repository.conflict_transaction():
            facts = self.repository.conflict_inputs(track_id)
            if facts is None:
                return
            identities = []
            for row in facts['conflicts']:
                concept = 'year' if row['field'] == 'mb_year' else row['field']
                if row['status'] == 0 and policy.agreed(self._candidates(row, at), evidence.get(concept, {})):
                    identities.append(row['id'])
            self.repository.mark_converged(facts['layout'], identities, at)

    def _candidates(self, row, now):
        evidence = row['evidence']
        if 'candidates' in evidence:
            return evidence['candidates']
        identity, revision = evidence['reference']
        document = self.files.read('conflicts', (identity,), revision)
        return policy.current_candidates(document, evidence['updated_at'], now)

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
        candidates = self._candidates(facts, at)
        if not policy.offered_value(facts['current'], candidates, value):
            return False
        self.resolve_conflict(facts['entry_id'], field, value, now=at)
        return True
