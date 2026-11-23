"""Application settings workflows with explicit SQL and file collaborators."""
from contextlib import ExitStack
import json

from yt_playlist.core import recommendation_setting_outputs as outputs
from yt_playlist.core import spotify_job_state, entity_names, taste_control_state
from yt_playlist.core import workflow_settings
from yt_playlist.controllers.workflow_settings import WorkflowSettings

FILE_SETTINGS = outputs.KEYS | taste_control_state.KEYS | {spotify_job_state.KEY}

class Settings:
    def __init__(self, repository, files, recovery):
        self.repository, self.files = repository, files
        self.recovery = recovery
        self.workflows = WorkflowSettings(repository, recovery)

    def get_setting(self, key, default=None):
        if workflow_settings.owns(key):
            return self.workflows.get(key, default)
        if key in taste_control_state.KEYS:
            with self.recovery.retained():
                document = self.repository.taste_document(key)
                if document is None:
                    return default
                row = document['header']
                reference = tuple(row[field] for field in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
                kind = 'taste-checkpoint' if key == taste_control_state.CHECKPOINT else 'taste-comparison'
                opaque = self.recovery.read(reference, kind=kind, owner=1)
                return taste_control_state.restore(key, document, opaque)
        if key == spotify_job_state.KEY:
            job = self._load_job()
            return json.dumps(entity_names.external_account_document(job)) if job is not None else default
        if key in outputs.KEYS:
            with self.files.retained():
                reference = self.repository.output_document(key)
                if reference is None:
                    return default
                try:
                    return self.files.read(reference, surface=key, chunk_key='')
                except (OSError, ValueError):
                    return default
        return self.repository.get_setting(key, default)

    def _write(self, values, writer):
        selected = {key: raw for key, raw in values.items() if key in outputs.KEYS}
        import_job = spotify_job_state.KEY in values
        controls = {key: raw for key, raw in values.items() if key in taste_control_state.KEYS}
        workflows = {key: raw for key, raw in values.items() if workflow_settings.owns(key)}
        if not (selected or import_job or controls or workflows):
            return writer(values)
        prepared = {key: outputs.prepare_record(key, raw) if raw else None for key, raw in selected.items()}
        raw_job = values.get(spotify_job_state.KEY)
        job = self._prepare_job(entity_names.account_document(json.loads(raw_job))) if raw_job else None
        taste = {key: taste_control_state.prepare(key, raw) for key, raw in controls.items()}
        with ExitStack() as stack:
            arguments = {}
            file_writes = []
            if workflows:
                stack.enter_context(self.recovery.retained())
                arguments['workflow_records'] = self.workflows.prepare_values(workflows)
            if selected:
                stack.enter_context(self.files.retained())
                durable, output_ids = {}, {}
                for key, output in prepared.items():
                    if output is None:
                        continue
                    integrity = self.files.describe(output['encoded'])
                    identity = self.repository.reserve_output_file(key, integrity)
                    durable[key] = self.files.publish_reserved(identity, output['encoded'], expected=integrity)
                    output_ids[key] = identity
                arguments['outputs'] = prepared, durable, output_ids
            if import_job:
                stack.enter_context(self.recovery.retained())
                arguments['import_job'] = job, self._publish_job(job) if job is not None else None
            if controls:
                stack.enter_context(self.recovery.retained())
                arguments['controls'] = {key: (value, self._publish_recovery(kind, 1, opaque, file_writes) if value is not None else None)
                                         for key, (value, kind, opaque) in taste.items()}
            if file_writes:
                arguments['file_writes'] = file_writes
            return writer(values, **arguments)

    def _publish_recovery(self, kind, owner, value, file_writes):
        integrity = self.recovery.describe(kind, owner, value)
        file_id = self.repository.reserve_file(kind, owner, integrity)
        reference = self.recovery.publish_reserved(file_id, kind, owner, value, expected=integrity)
        file_writes.append((file_id, kind, owner, reference))
        return reference

    def set_values(self, values):
        return self._write(values, self.repository.set_values)

    def publish_values(self, values):
        return self._write(values, self.repository.publish_values)

    def set_setting(self, key, value):
        if key in FILE_SETTINGS or workflow_settings.owns(key):
            return self.set_values({key: value})
        return self.repository.set_setting(key, value)

    def delete_setting(self, key):
        if key in FILE_SETTINGS or workflow_settings.owns(key):
            return self.set_values({key: None})
        return self.repository.delete_setting(key)

    def get_settings_with_prefix(self, prefix):
        with self.files.retained(), self.recovery.retained():
            values, workflow_keys = self.repository.prefix_documents(prefix)
            values.update((key, self.workflows.get(key)) for key in workflow_keys)
            return values

    def replace_keys(self, keys, values):
        return self.repository.replace_keys(keys, values)

    def spotify_import_job(self):
        return self._load_job(token=True)

    def spotify_import_status(self):
        return self.repository.import_job_status()

    def save_spotify_import_job(self, job):
        job = entity_names.account_document(job)
        prepared = self._prepare_job(job)
        with self.recovery.retained():
            return self.repository.save_spotify_import_job(prepared, self._publish_job(prepared))

    def _prepare_job(self, job):
        account = job.get('account_id') if isinstance(job, dict) else None
        return spotify_job_state.prepare(job, self.repository.import_job_facts(account))

    def _publish_job(self, prepared):
        job = prepared['value']
        value = {key: job[key] for key in spotify_job_state.OPAQUE if key in job}
        integrity = self.recovery.describe('spotify-import', prepared['identity'], value)
        prepared['file_id'] = self.repository.reserve_file('spotify-import', prepared['identity'], integrity)
        return self.recovery.publish_reserved(prepared['file_id'], 'spotify-import', prepared['identity'], value,
                                               expected=integrity)

    def _load_job(self, *, token=False):
        with self.recovery.retained():
            row = self.repository.import_job_document()
            if row is None:
                return None
            reference = tuple(row[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
            value = self.recovery.read(reference, kind='spotify-import', owner=row['id'])
            return spotify_job_state.restore(row, value, token=token)

    def spotify_transfer_receipts(self, account_id):
        return self.workflows.receipts(account_id, token=True)

    def save_spotify_transfer(self, account_id, fingerprint, value):
        return self.workflows.save_transfer(account_id, fingerprint, value)
