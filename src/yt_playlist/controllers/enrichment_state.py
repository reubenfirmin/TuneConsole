"""Coordinate provider evidence, retries and restartable configuration refreshes."""
import json
from uuid import uuid4

from yt_playlist.core import enrichment_state_policy as policy
from yt_playlist.core import enrichment_selection_policy as selections, metadata_native, metadata_values
from yt_playlist.core import native_metadata_policy as native
from yt_playlist.core import enrichment_conflict_policy as conflicts
from yt_playlist.core import legacy_journals
from yt_playlist.core import provider_queries
from yt_playlist.core.activity import check_cancellable_work, WorkBudgetExceeded
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.metadata_contract import Capture, clock
from yt_playlist.core.metadata_deliveries import Delivery
from yt_playlist.core.metadata_observations import SubjectChanged
from yt_playlist.core.metadata_runtime_files import MAX_BYTES

RECONCILE_BATCH = 64


class EnrichmentState:
    def __init__(self, repository, choices, files, delivery_files, deliveries, enrichment, provider_order, originals):
        self.repository, self.choices = repository, choices
        self.files, self.delivery_files = files, delivery_files
        self.deliveries = deliveries
        self.enrichment, self.provider_order = enrichment, provider_order
        self.originals = originals

    def subject_guard(self, entry_id, recording_id):
        return self.repository.subject_guard(entry_id, recording_id)

    def capture_provider_result(self, entry_id, provider, fields, *, run_id, now=None):
        inputs = self.repository.capture_inputs(entry_id)
        if inputs['layout'] == 'runtime':
            captured = selections.result_fields(provider, fields, run_id)
            for field, value in captured.items():
                metadata_values.prepare_value(field, value,
                    unit=metadata_values.DEFAULT_UNIT.get(field) if value not in (None, '') else None)
            encoded = canonical(fields)
            if len(encoded) > MAX_BYTES - 4096:
                raise ValueError('Provider result exceeds its byte budget')
            return Capture(inputs['entry_id'], inputs['recording_id'], inputs['video_id'], provider, encoded, run_id, clock(now)), {}
        if not inputs['attempts']:
            return None
        return self.choices.capture_result(inputs['entry_id'], provider, fields, run_id=run_id, now=now)

    def record_metadata_observations(self, entry_id, provider, observations, *, run_id, now=None, expected_recording_id=None):
        inputs = self.repository.capture_inputs(entry_id)
        if expected_recording_id is not None and expected_recording_id != inputs['recording_id']:
            raise SubjectChanged('Observation catalog subject changed during lookup')
        at = clock(now)
        prepared, groups = native.prepare(provider, observations, run_id, at, native=inputs['native'])
        if not inputs['native']:
            self.repository.store_legacy_observations(inputs, native.legacy_rows(inputs, provider, prepared, run_id, at))
            return
        for (scope, entity), values in groups.items():
            if inputs['layout'] == 'runtime':
                if scope in native.MODEL_SCOPES:
                    self._record_native_source(inputs, provider, scope, entity, values, run_id, at)
            else:
                self._record_native_delivery(inputs, provider, scope, entity, values, run_id, at)

    def _record_native_source(self, subject, provider, scope, entity, values, run_id, now):
        key = native.source_key(provider, scope, entity)
        with self.files.retained(), self.delivery_files.retained(), self.repository.state_transaction():
            inputs = self.repository.native_source_inputs(subject, key)
            source = inputs['source']
            previous = self.files.read('native', (key,), source['revision']) if source else {}
            head = inputs['head']
            if head is None:
                legacy = inputs['legacy']
                body = (self.delivery_files.read(tuple(legacy[name] for name in
                    ('raw_filename', 'raw_digest', 'raw_byte_size', 'raw_offset')))
                    if legacy and (previous.get('run_id') is None or previous.get('at') is None) else None)
                head = native.installed_head(previous, legacy, body)
            update = native.runtime_update(subject, provider, source, previous, head, values, run_id, now)
            if update is None:
                return
            if update['revision'] is not None:
                self.files.publish('native', (key,), update['revision'], update['document'],
                                   current_revision=source['revision'] if source else None)
            self.repository.store_native_source(subject, key, provider, scope, entity, update, run_id)

    def _record_native_delivery(self, subject, provider, scope, entity, values, run_id, now):
        identity = self.deliveries.record(Delivery(provider, run_id, scope, entity, now,
            subject['entry_id'], subject['recording_id'], subject['video_id'],
            response={'format': native.FORMAT, 'observations': values}))
        key = native.source_key(provider, scope, entity) if scope in native.MODEL_SCOPES else None
        with self.delivery_files.retained(), self.repository.state_transaction():
            for position, payload in enumerate(values):
                inputs = self.repository.native_observation_inputs(subject, identity, position, key, payload['field'])
                if inputs['existing']:
                    continue
                previous, changed = inputs['previous'], True
                if previous is not None:
                    body = self.delivery_files.read(tuple(previous[name] for name in
                        ('raw_filename', 'raw_digest', 'raw_byte_size', 'raw_offset')))
                    changed = native.profile_value(metadata_native.decode_observation(previous, body)) != native.profile_value(payload)
                self.repository.store_native_observation(subject, identity, position, payload, key, provider, run_id,
                                                         source_exists=inputs['source'], changed=changed)

    def metadata_observations(self, identity_key, *, after_id=0, limit=100):
        policy.observation_page_bounds(after_id, limit)
        with self.files.retained(), self.delivery_files.retained():
            inputs = self.repository.observation_inputs(identity_key, after_id=after_id, limit=limit)
            rows, layout = inputs['rows'], inputs['layout']
            if layout == 'runtime':
                result, used = [], 0
                for row in rows:
                    data = self.files.read('native', (row['source_key'],), row['revision'])
                    for observation in policy.runtime_observations(row, data, after_id):
                        used += len(canonical({'value': observation['value'], 'provenance': observation['provenance']}))
                        if used > MAX_BYTES:
                            raise WorkBudgetExceeded('Native metadata page exceeds byte budget')
                        result.append(observation)
                        if len(result) == limit:
                            return result
                return result
            if layout == 'native':
                if sum(row['raw_byte_size'] for row in rows) > MAX_BYTES:
                    raise WorkBudgetExceeded('Native metadata page exceeds byte budget')
                bodies, result = {}, []
                for row in rows:
                    if row['delivery_id'] not in bodies:
                        bodies[row['delivery_id']] = self.delivery_files.read(row['reference'])
                    payload = metadata_native.decode_observation(row, bodies[row['delivery_id']])
                    result.append(policy.native_observation(row, payload))
                return result
            if layout == 'prepared':
                result = []
                for reference in rows:
                    row = self.delivery_files.read(tuple(reference[name] for name in
                        ('filename', 'digest', 'byte_size', 'byte_offset')))['row']
                    result.append({**row, 'value': json.loads(row['value_json']),
                                   'provenance': json.loads(row['provenance_json'])})
                return result
            return rows

    def integration_cooldown(self, provider):
        return self.repository.integration_cooldown(provider)

    def defer_integration(self, provider, *, now, retry_after, error):
        with self.repository.state_transaction():
            result = policy.integration_retry(provider, self.repository.integration_cooldown(provider),
                                              now=now, retry_after=retry_after, error=error)
            self.repository.store_integration_cooldown(result)
        return result

    def clear_integration_cooldown(self, provider, admitted_deadline):
        return self.repository.clear_integration_cooldown(provider, admitted_deadline)

    def record_enrichment_attempt(self, identity_key, provider, fields, *, retry=False, now=None,
                                  video_id=None, error='', retry_after=0, providers=(), run_id=None, delivery_id=None):
        at, retry_after = float(clock(now)), clock(retry_after)
        status = policy.attempt_status(fields, retry)
        entry = delivery_id.entry if isinstance(delivery_id, Capture) else None
        inputs = self.repository.attempt_context(identity_key, provider, video_id, entry_id=entry)
        subject = inputs['subject']
        if subject is None:
            if inputs['layout'] == 'runtime' or 'recording_id' in inputs:
                raise ValueError('Provider attempt needs a current catalog input')
            self._retain_unresolved_attempt(identity_key, provider, {'at': at, 'status': status, 'fields': fields}, video_id)
            return
        observations = {}
        if delivery_id is None and inputs['layout'] != 'legacy':
            delivery_id, observations = self.capture_provider_result(subject['entry_id'], provider, fields, run_id=run_id, now=at)
        if inputs['layout'] == 'evidence':
            observations = self.choices.result_evidence(subject['entry_id'], provider, delivery_id, fields, complete=True, run_id=run_id)
        with self.files.retained(), self.repository.state_transaction():
            current = self.repository.attempt_context(identity_key, provider, video_id, entry_id=subject['entry_id'],
                delivery_id=delivery_id if inputs['layout'] == 'evidence' else None)
            if current['subject'] != subject or current['layout'] != inputs['layout']:
                raise SubjectChanged('Provider attempt catalog subject changed during lookup')
            if current['layout'] == 'runtime':
                selections.validate_capture(subject, provider, fields, delivery_id, run_id=run_id, complete=True)
                previous = current['previous']
                prior = self._attempt_alternatives(current, at)
                if policy.stale_attempt(previous, prior, delivery_id, run_id, at):
                    return
                retained = selections.scoped_fields(subject, prior)
            else:
                if current['layout'] == 'evidence' and policy.attempt_replayed(current['attempt'], status):
                    return
                retained = json.loads(current['previous']['fields_json']) if current['layout'] == 'legacy' and current['previous'] else {}
            result = policy.attempt_state(current['previous'], current['cycle'], fields, now=at, retry=retry,
                retry_after=retry_after, error=error, prior=retained, runtime=current['layout'] == 'runtime')
            if current['layout'] == 'runtime':
                provider_id = current['provider_id']
                if provider_id is None:
                    provider_id = self.repository.reserve_provider_identity(provider)
                self.files.publish('alternatives', (subject['recording_id'], provider_id), at,
                    policy.attempt_document(subject, provider, result, run_id, error))
            self.repository.store_attempt(current, provider, fields, result, delivery_id=delivery_id,
                observations=observations, video_id=video_id)
        if not retry:
            self._complete_provider(subject['recording_id'], provider, fields, providers, at)

    def _retain_unresolved_attempt(self, identity_key, provider, result, video_id):
        source_id, reason = uuid4().hex, 'unresolved input identity'
        payload = json.dumps({'identity_key': identity_key, 'video_id': video_id, 'provider': provider, **result})
        with self.originals.retained(), self.repository.state_transaction():
            inputs = self.repository.unresolved_attempt_inputs(source_id)
            if inputs['reference'] is not None:
                self.originals.read_receipt('metadata', inputs['reference'])
                return
            publication = reference = None
            if inputs['layout'] == 'journal':
                key = legacy_journals.key('enrichment_unresolved_attempt', source_id)
                if key in self.originals.journal_entries('metadata', inputs['publication'], [key]):
                    return
                retention, basis = legacy_journals.disposition('metadata', 'enrichment_unresolved_attempt', payload, reason)
                publication = self.originals.publish_journal('metadata', inputs['publication'], {key: {
                    'payload': payload, 'reason': reason, 'archived_at': clock(None), 'subjects': {},
                    'state': 'retained', 'retention': retention, 'disposition': basis}})
            elif inputs['layout'] == 'receipt':
                reference, = self.originals.publish_receipts('metadata', [payload])
            self.repository.store_unresolved_attempt(inputs, identity_key, json.dumps({provider: result}),
                publication=publication, reference=reference)

    def _attempt_alternatives(self, inputs, now):
        previous = inputs['previous']
        if previous is None or not policy.alternative_available({'provider_id': inputs['provider_id'], 'state': previous}, now):
            return {}
        return self.files.read('alternatives', (inputs['recording_id'], inputs['provider_id']), float(previous['at']), missing=True) or {}

    def _complete_provider(self, recording_id, provider, fields, providers, now):
        with self.files.retained(), self.delivery_files.retained():
            inputs = self.repository.completion_inputs(recording_id, provider)
            if inputs is None:
                return
            answers = (dict(inputs['answers']) if 'answers' in inputs else
                {name: state['fields'].get('genre') for name, state in self._provider_states(inputs['states'], now).items()})
        answers[provider] = fields.get('genre')
        self.enrichment.refresh_genre(recording_id, answers, providers or self.provider_order(), now=now)
        self.repository.finish_provider_job(recording_id, provider)

    def enrichment_provider_state(self, recording_id, *, include_previous=False, entry_id=None):
        with self.files.retained(), self.delivery_files.retained():
            inputs = self.repository.provider_state_inputs(recording_id, include_previous=include_previous, entry_id=entry_id)
            return self._provider_states(inputs, clock(None))

    def _provider_states(self, inputs, now, *, video_id=None):
        result = {}
        for row in inputs['rows']:
            state = row['state']
            if inputs['runtime']:
                data = (self.files.read('alternatives', (state['recording_id'], row['provider_id']),
                    float(state['at']), missing=True) or {}) if policy.alternative_available(row, now) else {}
                if video_id is not None and data.get('input_video_id') != video_id:
                    continue
                fields = selections.scoped_fields(inputs['subject'], data) if inputs['subject'] else data.get('fields', {})
                state = {**state, 'error': data.get('error', '')}
            else:
                fields = self._fields(row['fields'])
            result[state['provider']] = {**state, 'fields': fields}
        return result

    def _fields(self, inputs):
        if 'prepared' in inputs:
            reference = inputs['prepared']
            return policy.prepared_fields(self.delivery_files.read(reference['reference']), reference['fields'])
        if 'deliveries' in inputs:
            rows = inputs['deliveries']
            bodies = {identity: selections.delivery_fields(self.delivery_files.read(reference))
                      for identity, reference in policy.field_deliveries(rows).items()}
            return policy.referenced_fields(rows, bodies)
        return inputs['values']

    def expire_provider_alternatives(self, now=None):
        now = clock(now)
        if self.repository.expiry_state() is None:
            return 0
        with self.files.collecting() as acquired:
            if not acquired:
                return 0
            with self.repository.state_transaction():
                state = self.repository.expiry_state()
                removed, shard = self.files.expire_alternatives(state['gc_shard'], now)
                self.repository.store_expiry_shard(shard)
                if state['gc_after'] > now:
                    return removed
                rows = self.repository.expiry_page(state['gc_cursor'], limit=16)
                for row in rows:
                    for conflict in row['conflicts']:
                        removed += self._expire_conflict(conflict, now)
                    for source in row['sources']:
                        removed += self.files.prune_revisions('native', (source['source_key'],), (source['revision'],))
                self.repository.store_expiry_cursor(rows[-1]['recording_id'] if rows else 0, 0 if rows else now + 3600)
                return removed

    def _expire_conflict(self, row, now):
        identity, revision = row['id'], row['revision']
        data = self.files.read('conflicts', (identity,), revision)
        current = {**data, 'candidates': conflicts.current_candidates(data, row['updated_at'], now)}
        keep = (revision,)
        if current != data:
            self.files.publish('conflicts', (identity,), revision + 1, current, current_revision=revision)
            self.repository.store_conflict_revision(identity, revision + 1)
            keep = revision, revision + 1
        return self.files.prune_revisions('conflicts', (identity,), keep)

    def enrichment_cooldowns(self):
        return self.repository.enrichment_cooldowns()

    def enrichment_retry_status(self):
        row = self.repository.retry_status_inputs('lastfm', provider_queries.lastfm_ready_sql())
        return {'count': row['count'] if row else 0, 'retry_at': row['retry_at'] if row else None, 'error': row['error'] if row else ''}

    def record_artwork_result(self, entry_id, release_ids, outcome, now):
        with self.repository.state_transaction():
            result = policy.artwork_retry(self.repository.artwork_state(entry_id), outcome, now)
            self.repository.store_artwork_result(entry_id, release_ids, result)

    def due_artwork_retries(self, now, limit=1):
        return self.repository.due_artwork_retries(now, limit=limit)

    def artwork_retry_status(self):
        return self.repository.artwork_retry_status()

    def reconcile_provider_configuration(self, configuration):
        self._configure_providers(configuration)
        for provider, current in configuration.items():
            if current['status'] != 'available':
                continue
            while self._advance_provider_refresh(provider, current):
                pass
            cursor = 0
            while True:
                check_cancellable_work()
                candidates = self.repository.missing_provider_coverage(provider, after_id=cursor, limit=RECONCILE_BATCH)
                if not candidates or not self.repository.admit_provider_coverage(provider, current, candidates):
                    break
                cursor = candidates[-1]

    def _configure_providers(self, configuration):
        check_cancellable_work()
        if not policy.configuration_changes(configuration, self.repository.configuration_inputs(configuration)):
            return
        with self.repository.state_transaction():
            inputs = self.repository.configuration_inputs(configuration)
            changes = policy.configuration_changes(configuration, inputs)
            cutoff = int(clock(None) * 1_000_000) if inputs['runtime'] else inputs['last_attempt']
            self.repository.store_configuration_changes(changes, cutoff)

    def _advance_provider_refresh(self, provider, current):
        check_cancellable_work()
        if not self.repository.provider_refresh_pending(provider):
            return False
        with self.repository.state_transaction():
            inputs = self.repository.refresh_inputs(provider, RECONCILE_BATCH)
            refresh = inputs['frontier']
            if inputs['configuration'] != current or refresh is None:
                return False
            if refresh['fingerprint'] != current['fingerprint']:
                raise RuntimeError('Provider refresh does not match its configuration')
            if not inputs['rows']:
                self.repository.clear_provider_refresh(provider)
                return False
            stale, jobs = policy.refresh_page(inputs['rows'], refresh['cutoff'], runtime=inputs['runtime'])
            self.repository.store_refresh_page(provider, refresh, stale, jobs, inputs['rows'][-1]['id'])
        return True

    def provider_refresh_pending(self, provider):
        return self.repository.provider_refresh_pending(provider)

    def enrichment_provider_jobs(self, recording_id):
        return self.repository.enrichment_provider_jobs(recording_id)

    def provider_coverage(self, recording_id):
        return {row['provider']: policy.coverage_status(row['configured'], row['status'], row['pending'])
                for row in self.repository.coverage_inputs(recording_id)}

    def enrichment_attempts(self, identity_key, video_id=None):
        with self.files.retained(), self.delivery_files.retained():
            inputs = self.repository.attempt_inputs(identity_key, video_id)
            if inputs['runtime']:
                return self._provider_states(inputs, clock(None), video_id=video_id)
            return {row['provider']: {'at': row['at'], 'status': row['status'], 'fields': self._fields(row['fields'])}
                    for row in inputs['rows']}

    def log_enrichment(self, track_id, run_id, provider, field, value, now=None, *, delivery_id=None):
        inputs, at = self.repository.capture_inputs(track_id), clock(now)
        observation = None
        if inputs['layout'] == 'runtime':
            selections.validate_capture(inputs, provider, {field: value}, delivery_id, run_id=run_id)
        elif inputs['attempts']:
            if delivery_id is None:
                delivery_id, observations = self.choices.capture_result(inputs['entry_id'], provider, {field: value},
                    run_id=run_id, now=at, part='log:' + field)
            else:
                observations = self.choices.result_evidence(inputs['entry_id'], provider, delivery_id, {field: value}, run_id=run_id)
            observation = observations.get('mb_year' if field == 'year' else field)
        scheme, identifiers = policy.identifiers(field, value, strict=inputs['layout'] == 'evidence')
        self.repository.store_enrichment_log(inputs, run_id, provider, field, value, at, scope=policy.log_scope(field),
            scheme=scheme, identifiers=identifiers, delivery_id=delivery_id if inputs['attempts'] else None, observation_id=observation)
        return observation
