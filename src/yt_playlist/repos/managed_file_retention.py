"""SQL ownership checks for bounded managed-file collection; no file access."""
from contextlib import contextmanager

from yt_playlist.core import album_description_files, legacy_evidence, metadata_locator_contract
from yt_playlist.core.db_writes import atomic_write, reserved_write
from .base import Repo, synchronized
from . import metadata_file_writes
from .playback_inbox import owns_file as inbox_owns_file

RELATIONS = {
    'policies': (('recommendation_policy_bodies', 'configuration_filename'),
                 ('recommendation_policies', 'configuration_filename')),
    'annotations': (('audio_annotations', 'model_output_filename'),),
    'descriptions': tuple((table, fields[1]) for table, fields in album_description_files.OWNERS.items()),
    'archives': tuple((table, 'filename') for table in legacy_evidence.DOMAINS.values()),
}


class ManagedFileRetentionRepo(Repo):
    def __init__(self, database, inbox_path):
        super().__init__(database)
        self.inbox_path = inbox_path

    @contextmanager
    def reservation(self, domain):
        # Installed writers still require SQL admission in addition to the file
        # fence. This reservation touches no other repository or domain mutation.
        if domain == 'playback-inbox':
            # Its independent SQL queue and native writers use this root's
            # reader fence. Never reserve the library writer across inbox SQL.
            yield
        else:
            with reserved_write(self._store):
                yield

    @synchronized
    def required(self, domain, path, owner=None):
        if domain == 'playback-inbox':
            return inbox_owns_file(self.inbox_path, path)
        if domain == 'metadata-runtime':
            return self._metadata_required(owner)
        if domain == 'metadata-deliveries':
            return (not metadata_locator_contract.retired(self.conn) and self.conn.execute(
                'SELECT 1 FROM metadata_deliveries WHERE raw_filename=? LIMIT 1', (str(path),)).fetchone() is not None)
        if domain == 'samples':
            columns = {row[1] for row in self.conn.execute('PRAGMA main.table_info(sample_attributes)')}
            ready = " AND file_status='ready'" if 'file_status' in columns else ''
            return self.conn.execute('SELECT 1 FROM sample_attributes WHERE filename=?' + ready + ' LIMIT 1',
                                     (str(path),)).fetchone() is not None
        return any(self.conn.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1',
                    (str(path),)).fetchone() is not None
                   for table, column in RELATIONS[domain]
                   if self.conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (table,)).fetchone())

    def _metadata_required(self, owner):
        if owner is None:
            return True
        if owner[0] == 'receipt':
            return metadata_file_writes.available(self.conn) and self.conn.execute(
                'SELECT 1 FROM metadata_file_references WHERE file_id=? LIMIT 1', (owner[1],)).fetchone() is not None
        kind, keys, revision = owner
        if not self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='metadata_runtime_state'").fetchone():
            return True
        if kind == 'alternatives':
            row = self.conn.execute('SELECT s.at FROM enrichment_provider_state s JOIN metadata_runtime_providers p '
                'ON p.name=s.provider WHERE s.recording_id=? AND p.id=?', keys).fetchone()
        else:
            table, key = {'native': ('rec_musical_sources', 'source_key'),
                          'choices': ('metadata_runtime_choices', 'id'),
                          'conflicts': ('metadata_runtime_conflicts', 'id')}[kind]
            row = self.conn.execute(f'SELECT revision FROM {table} WHERE {key}=?', keys).fetchone()
        return row is not None and (1 <= revision <= row[0] if kind == 'choices' else row[0] == revision)

    def maintain_history(self, now):
        from yt_playlist.core import credit_retention, mode_retention
        progress = {}
        for domain, owner in (('credits', credit_retention), ('modes', mode_retention)):
            with atomic_write(self._store):
                progress[domain] = owner.step(self.conn, now, activate=False)
        return progress
