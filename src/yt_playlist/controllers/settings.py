"""Application settings workflows with explicit SQL and file collaborators."""
from contextlib import ExitStack
import hashlib
import json

from yt_playlist.core import recommendation_setting_outputs as outputs
from yt_playlist.core import model_state, spotify_job_state, entity_names, taste_control_state, experiment_decision_state
from yt_playlist.core import workflow_settings
from yt_playlist.core.radio_reads import ORIGIN_KEYS
from yt_playlist.controllers.workflow_settings import WorkflowSettings
from yt_playlist.core.schema_contract import MigrationError

PREPARED_KEYS = frozenset({spotify_job_state.KEY, *taste_control_state.KEYS, *experiment_decision_state.KEYS,
    'library_sync_state', 'listening_playback_bindings', 'listening_album_pending',
    'album_feedback', 'auth_expired_identities', 'radio_current_session', *ORIGIN_KEYS})


class Settings:
    def __init__(self, repository, files, recovery):
        self.repository, self.files = repository, files
        self.recovery = recovery
        self.workflows = WorkflowSettings(repository, recovery)

    def get_setting(self, key, default=None):
        if workflow_settings.owns(key) and self.repository.has_transfer_receipts():
            return self.workflows.get(key, default)
        if key in experiment_decision_state.KEYS and self.repository.native_decisions():
            with self.recovery.retained():
                _, records = self._decision_state()
                return json.dumps(records) if records else default
        if key in taste_control_state.KEYS and self.repository.native_taste_controls():
            with self.recovery.retained():
                document = self.repository.taste_document(key)
                if document is None:
                    return default
                row = document['header']
                reference = tuple(row[field] for field in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
                kind = 'taste-checkpoint' if key == taste_control_state.CHECKPOINT else 'taste-comparison'
                opaque = self.recovery.read(reference, kind=kind, owner=1)
                return taste_control_state.restore(key, document, opaque)
        if key == spotify_job_state.KEY and self.repository.native_import_jobs():
            job = self._load_job()
            return json.dumps(entity_names.external_account_document(job)) if job is not None else default
        if key in model_state.FIELDS:
            with self.recovery.retained(), self.files.retained():
                raw, document = self.repository.model_document(key, default)
                if document is None:
                    return raw
                if document['storage'] == 'collection':
                    return self.recovery.read(document['reference'], kind='taste-settings', owner=document['owner'])
                parts = document['parts']
                if [part['chunk_key'] for part in parts] != list(map(str, range(document['part_count']))):
                    raise MigrationError('Prepared content encoder original parts are incomplete')
                raw = ''.join(self.files.read(part, surface=key, chunk_key=part['chunk_key']) for part in parts)
                if (len(raw.encode()) != document['raw_size'] or
                        hashlib.sha256(raw.encode()).hexdigest() != document['raw_digest']):
                    raise MigrationError('Prepared content encoder differs from its original digest')
                return default if document['source_kind'] == 'null' or not raw else raw
        if key in outputs.KEYS:
            with self.files.retained():
                handled, reference = self.repository.output_document(key)
                if handled:
                    if reference is None:
                        return default
                    try:
                        raw = self.files.read(reference, surface=key, chunk_key='')
                    except (OSError, ValueError):
                        if reference['prepared']:
                            raise
                        return default
                    if reference['prepared']:
                        kind = 'null' if raw is None else 'blank' if raw == '' else 'recorded'
                        if (kind != reference['source_kind'] or raw is not None and
                                (not isinstance(raw, str) or len(raw.encode()) != reference['raw_size'])):
                            raise MigrationError('Prepared saved output differs from its captured kind or byte size')
                    return raw
        if key in PREPARED_KEYS or workflow_settings.owns(key):
            with self.files.retained():
                handled, raw, document = self.repository.prepared_document(key, default)
                if handled:
                    return raw if document is None else self._read_prepared(document)
        return self.repository.get_setting(key, default)

    def _read_prepared(self, document):
        if document['storage'] == 'plain':
            return self.files.read_original(document)
        parts = document['parts']
        if [part['position'] for part in parts] != list(range(document['part_count'])):
            raise MigrationError('Prepared setting original parts are incomplete')
        values = [self.files.read(part, surface=document['surface'],
                  chunk_key=document['prefix']+str(part['position'])) for part in parts]
        null = document.get('source_kind') == 'null'
        if null and document['nullable_part']:
            if values != [None]:
                raise MigrationError('Prepared setting has an invalid null source')
            raw = ''
        else:
            raw = ''.join(values)
        encoded = raw.encode()
        if len(encoded) != document['raw_size'] or hashlib.sha256(encoded).hexdigest() != document['raw_digest']:
            raise MigrationError('Prepared setting differs from its captured bytes')
        return None if null else raw

    def _write(self, values, writer):
        selected = {key: raw for key, raw in values.items() if key in outputs.KEYS}
        saved = bool(selected) and self.repository.saved_outputs_native()
        models = {key: raw for key, raw in values.items() if key in model_state.FIELDS}
        native_models = bool(models) and self.repository.native_models()
        native_job = spotify_job_state.KEY in values and self.repository.native_import_jobs()
        controls = {key: raw for key, raw in values.items() if key in taste_control_state.KEYS}
        native_controls = bool(controls) and self.repository.native_taste_controls()
        native_decisions = experiment_decision_state.KEY in values and self.repository.native_decisions()
        workflows = {key: raw for key, raw in values.items() if workflow_settings.owns(key)}
        native_workflows = bool(workflows) and self.repository.has_transfer_receipts()
        if not saved and not native_models and not native_job and not native_controls and not native_decisions and not native_workflows:
            return writer(values)
        prepared = {key: outputs.prepare_record(key, raw) if raw else None for key, raw in selected.items()} if saved else {}
        model_values = model_state.prepare_models(models) if native_models else {}
        job = self._prepare_job(entity_names.account_document(json.loads(values[spotify_job_state.KEY]))) if native_job and values[spotify_job_state.KEY] else None
        taste = {key: taste_control_state.prepare(key, raw) for key, raw in controls.items()} if native_controls else {}
        with ExitStack() as stack:
            arguments = {}
            if native_workflows:
                stack.enter_context(self.recovery.retained())
                arguments['workflow_records'] = self.workflows.prepare_values(workflows)
            if native_decisions:
                stack.enter_context(self.recovery.retained())
                rows, records = self._decision_state()
                first_id = rows[-1]['id']+1 if rows else 1
                appended = experiment_decision_state.prepare(values[experiment_decision_state.KEY], records)
                arguments['decisions'] = [row['id'] for row in rows], [
                    (first_id+offset, item, self.recovery.publish('experiment-decision', first_id+offset, item['live_evidence']))
                    for offset, item in enumerate(appended)]
            if saved:
                stack.enter_context(self.files.retained())
                durable = {key: self.files.publish(output['encoded'])
                           for key, output in prepared.items() if output is not None}
                arguments['outputs'] = prepared, durable
            if native_models:
                stack.enter_context(self.recovery.retained())
                arguments['model_references'] = {owner: self.recovery.publish('taste-settings', owner, value)
                                                for owner, value in model_values.items()}
            if native_job:
                stack.enter_context(self.recovery.retained())
                arguments['import_job'] = job, self._publish_job(job) if job is not None else None
            if native_controls:
                stack.enter_context(self.recovery.retained())
                arguments['controls'] = {key: (value, self.recovery.publish(kind, 1, opaque) if value is not None else None)
                                         for key, (value, kind, opaque) in taste.items()}
            return writer(values, **arguments)

    def set_values(self, values):
        return self._write(values, self.repository.set_values)

    def publish_values(self, values):
        return self._write(values, self.repository.publish_values)

    def set_setting(self, key, value):
        if workflow_settings.owns(key) and self.repository.has_transfer_receipts():
            return self.set_values({key: value})
        if key in experiment_decision_state.KEYS and self.repository.native_decisions():
            return self.set_values({key: value})
        if key in taste_control_state.KEYS and self.repository.native_taste_controls():
            return self.set_values({key: value})
        if key == spotify_job_state.KEY:
            return self.set_values({key: value})
        if key in model_state.FIELDS and self.repository.native_models():
            return self.set_values({key: value})
        if key in outputs.KEYS and self.repository.saved_outputs_native():
            return self.set_values({key: value})
        return self.repository.set_setting(key, value)

    def delete_setting(self, key):
        if workflow_settings.owns(key) and self.repository.has_transfer_receipts():
            return self.set_values({key: None})
        if key in experiment_decision_state.KEYS and self.repository.native_decisions():
            return self.set_values({key: None})
        if key in taste_control_state.KEYS and self.repository.native_taste_controls():
            return self.set_values({key: None})
        if key == spotify_job_state.KEY and self.repository.native_import_jobs():
            return self.set_values({key: None})
        if key in model_state.FIELDS and self.repository.native_models():
            return self.set_values({key: None})
        if key in outputs.KEYS and self.repository.saved_outputs_native():
            return self.set_values({key: None})
        return self.repository.delete_setting(key)

    def get_settings_with_prefix(self, prefix):
        with self.files.retained(), self.recovery.retained():
            values, documents, native = self.repository.prefix_documents(prefix)
            values.update((key, self._read_prepared(document)) for key, document in documents.items())
            values.update((key, self.workflows.get(key)) for key in native)
            return values

    def gc_retry_deadlines(self):
        """Admission facts need no recovery-file reads; due attempts still read their evidence."""
        return self.repository.gc_retry_deadlines()

    def replace_keys(self, keys, values):
        return self.repository.replace_keys(keys, values)

    def _decision_state(self):
        rows = self.repository.decision_documents()
        records = [experiment_decision_state.restore(row, self.recovery.read(
            tuple(row[field] for field in ('recovery_filename', 'recovery_digest', 'recovery_bytes')),
            kind='experiment-decision', owner=row['id'])) for row in rows]
        return rows, records

    def spotify_import_job(self):
        if self.repository.native_import_jobs():
            return self._load_job(token=True)
        return entity_names.account_document(json.loads(self.get_setting(spotify_job_state.KEY) or 'null'))

    def save_spotify_import_job(self, job):
        job = entity_names.account_document(job)
        if not self.repository.native_import_jobs():
            return self.set_setting(spotify_job_state.KEY,
                json.dumps(entity_names.external_account_document(job), sort_keys=True))
        prepared = self._prepare_job(job)
        with self.recovery.retained():
            return self.repository.save_spotify_import_job(prepared, self._publish_job(prepared))

    def _prepare_job(self, job):
        account = job.get('account_id') if isinstance(job, dict) else None
        return spotify_job_state.prepare(job, self.repository.import_job_facts(account))

    def _publish_job(self, prepared):
        job = prepared['value']
        return self.recovery.publish('spotify-import', prepared['identity'],
            {key: job[key] for key in spotify_job_state.OPAQUE if key in job})

    def _load_job(self, *, token=False):
        with self.recovery.retained():
            row = self.repository.import_job_document()
            if row is None:
                return None
            reference = tuple(row[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
            value = self.recovery.read(reference, kind='spotify-import', owner=row['id'])
            return spotify_job_state.restore(row, value, token=token)

    def has_transfer_receipts(self):
        return self.repository.has_transfer_receipts()

    def spotify_transfer_receipts(self, account_id):
        return self.workflows.receipts(account_id, token=True)

    def save_spotify_transfer(self, account_id, fingerprint, value):
        return self.workflows.save_transfer(account_id, fingerprint, value)
