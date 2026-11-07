"""Coordinate provider evidence, retries and restartable configuration refreshes."""
from yt_playlist.core import enrichment_state_policy as policy
from yt_playlist.core import enrichment_selection_policy as selections, metadata_values
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.metadata_contract import Capture, clock
from yt_playlist.core.metadata_runtime_files import MAX_BYTES

RECONCILE_BATCH = 64


class EnrichmentState:
    def __init__(self, repository, choices):
        self.repository, self.choices = repository, choices

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

    def record_metadata_observations(self, entry_id, provider, observations, **options):
        return self.repository.record_metadata_observations(entry_id, provider, observations, **options)

    def metadata_observations(self, identity_key, *, after_id=0, limit=100):
        return self.repository.metadata_observations(identity_key, after_id=after_id, limit=limit)

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

    def record_enrichment_attempt(self, identity_key, provider, fields, **options):
        return self.repository.record_enrichment_attempt(identity_key, provider, fields, **options)

    def enrichment_provider_state(self, recording_id, *, include_previous=False, entry_id=None):
        return self.repository.enrichment_provider_state(recording_id, include_previous=include_previous, entry_id=entry_id)

    def expire_provider_alternatives(self, now=None):
        return self.repository.expire_provider_alternatives(now)

    def enrichment_cooldowns(self):
        return self.repository.enrichment_cooldowns()

    def enrichment_retry_status(self):
        return self.repository.enrichment_retry_status()

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
            candidates = self.repository.missing_provider_coverage(provider)
            for start in range(0, len(candidates), RECONCILE_BATCH):
                check_cancellable_work()
                if not self.repository.admit_provider_coverage(provider, current, candidates[start:start + RECONCILE_BATCH]):
                    break

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
        return self.repository.enrichment_attempts(identity_key, video_id)

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
