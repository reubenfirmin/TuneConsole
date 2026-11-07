"""Publish explicit metadata decisions before installing their SQL head."""
from dataclasses import replace
from collections.abc import Mapping
from uuid import uuid4

from yt_playlist.core.metadata_contract import clock
from yt_playlist.core import metadata_values, enrichment_selection_policy
from yt_playlist.core.metadata_deliveries import Delivery
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.util.matching import normalize


class MetadataChoices:
    def __init__(self, repository, files, deliveries):
        self.repository, self.files, self.deliveries = repository, files, deliveries

    def _prepare(self, field, value):
        prepared = metadata_values.prepare_value(field, value,
            unit=metadata_values.DEFAULT_UNIT.get(field) if value not in (None, '') else None)
        if field == 'genre' and prepared.kind == 'value':
            matches = self.repository.genre_matches(value, normalize(value))
            prepared = replace(prepared, genre_key=next(iter(matches)) if len(matches) == 1 else None,
                resolution_reason=None if len(matches) == 1 else 'ambiguous_genre' if matches else 'unknown_genre')
        return prepared

    def choose(self, entry, field, value=None, *, withdraw=False, source='user', now=None):
        metadata_values.scope(field)
        if type(withdraw) is not bool or not isinstance(source, str) or not source:
            raise ValueError('Invalid metadata choice operation or source')
        at = clock(now)
        prepared = metadata_values.Value('withdrawn') if withdraw else self._prepare(field, value)
        with self.repository.transaction():
            inputs = self.repository.input(entry, field)
            track = inputs['track']
            identity = revision = None
            if inputs['layout'] == 'runtime':
                previous = inputs['choice']
                identity = previous['id'] if previous else self.repository.reserve_identity()
                revision = previous['revision'] + 1 if previous else 1
                self.files.publish('choices', (identity,), revision, {
                    'entry_id': track['id'], 'recording_id': track['recording_id'],
                    'input_video_id': track['video_id'], 'field': field, 'value': value,
                    'withdrawn': withdraw, 'source': source, 'at': at},
                    current_revision=previous['revision'] if previous else None)
            result = self.repository.store(inputs, field, value, prepared, withdraw=withdraw,
                source=source, at=at, identity=identity, revision=revision)
            self._resume_query(field, track)
            return result

    def select(self, entry, field, value, source, *, now=None, observation_id=None):
        at = clock(now)
        prepared = self._prepare(field, value)
        inputs = self.repository.input(entry, field)
        delivery = None
        if inputs['layout'] == 'evidence' and observation_id is None:
            delivery = self.deliveries.capture_state(inputs['track']['id'], source, 'selected-field:' + field,
                {'format': 'catalog-field-capture-1', 'field': field, 'value': value}, now=at)
        with self.repository.transaction():
            self.repository.store_base(inputs, field, value, prepared, source, at,
                                       delivery_id=delivery, observation_id=observation_id)
            self._resume_query(field, inputs['track'])

    def capture_fields(self, entry, provider, fields, *, purpose, body, now=None):
        if not isinstance(fields, Mapping) or len(fields) > len(metadata_values.ITEM_FIELDS) + len(metadata_values.RECORDING_FIELDS):
            raise ValueError('Metadata evidence exceeds its finite field budget')
        prepared = {field: self._prepare(field, value) for field, value in fields.items()}
        delivery = self.deliveries.capture_state(entry, provider, purpose, body, now=clock(now))
        return delivery, self.repository.store_observations(delivery, prepared)

    def capture_result(self, entry, provider, fields, *, run_id=None, now=None, part='result'):
        captured = enrichment_selection_policy.result_fields(provider, fields, run_id)
        prepared = {field: self._prepare(field, value) for field, value in captured.items()}
        track = self.repository.input(entry, 'title')['track']
        delivery = Delivery(provider, 'enrichment-' + part + ':' + (run_id or uuid4().hex),
            'item' if track['video_id'] else 'recording', track['video_id'] or 'catalog-recording:' + str(track['recording_id']),
            clock(now), track['id'], track['recording_id'], track['video_id'],
            response={'format': 'enrichment-result-1', 'fields': dict(fields)})
        identity = self.deliveries.record(delivery)
        return identity, self.repository.store_observations(identity, prepared, source_run_id=run_id)

    def result_evidence(self, entry, provider, delivery, fields, *, run_id=None, complete=False):
        track = self.repository.input(entry, 'title')['track']
        evidence = self.repository.delivery_evidence(delivery)
        enrichment_selection_policy.validate_delivery(track, provider, evidence, run_id=run_id)
        supplied = enrichment_selection_policy.delivery_fields(self.deliveries.response(delivery))
        enrichment_selection_policy.validate_fields(supplied, fields)
        if complete and canonical(supplied) != canonical(fields):
            raise ValueError('Provider fields differ from their supplying result')
        return evidence['observations']

    def reuse_recording_observation(self, entry, observation, *, now=None):
        return self.repository.transfer_recording_observation(entry, observation, clock(now))

    def reuse_recording_selections(self, previous, target):
        inputs = self.repository.recording_reuse_inputs(previous, target)
        for field in enrichment_selection_policy.reusable_recording_fields(inputs):
            if inputs['layout'] == 'runtime':
                self.select(inputs['target_entry'], field, inputs['before'][field], inputs['sources'].get(field) or 'legacy_unknown')
            else:
                self.repository.copy_recording_selection(inputs['layout'], previous, target, field)

    def fill_import(self, entry, fields, source, *, hint_source, now=None):
        at = clock(now)
        prepared = {field: self._prepare(field, value) for field, value in fields.items()}
        inputs = self.repository.import_inputs(entry, hint_source)
        for field, value in fields.items():
            if value is not None and inputs['track'][field] is None:
                self.select(entry, field, value, source, now=at)
            if value is None or inputs['layout'] == 'runtime' or field in inputs['hints']:
                continue
            observation = None
            if inputs['layout'] == 'evidence':
                delivery = self.deliveries.capture_state(entry, 'catalog_import', 'hint:' + field + ':' + hint_source,
                    {'format': 'catalog-import-hint-1', 'source': hint_source, 'field': field, 'value': value}, now=at)
                observation = self.repository.store_observations(delivery, {field: prepared[field]})[field]
            self.repository.store_hint(inputs['layout'], entry, field, hint_source, value, observation)

    def restore(self, entry, field, value, *, now=None):
        self.select(entry, field, value, 'youtube', now=now)
        return self.choose(entry, field, withdraw=True, now=now)

    def _resume_query(self, field, track):
        if field in ('title', 'artist'):
            current = self.repository.query_identity(track['id'])
            if current is not None and current != (track['recording_id'], track['title'], track['artist']):
                self.repository.resume_provider_query(current[0], 'lastfm', status='retry')
