"""SQL ownership checks for bounded managed-file collection; no file access."""
from contextlib import contextmanager

from yt_playlist.core import legacy_journals, paid_description_access, policy_bodies, runtime_metadata_choices
from yt_playlist.core.db_writes import atomic_write, reserved_write
from .base import Repo, synchronized
from . import metadata_file_writes

RELATIONS = {
    'policies': ((policy_bodies.VIEW, 'configuration_filename'),
                 ('recommendation_policies', 'configuration_filename')),
    'descriptions': (('album_description_requests', 'evidence_filename'),),
    'archives': (('archive_evidence_files', 'filename'),),
}


class ManagedFileRetentionRepo(Repo):
    @contextmanager
    def reservation(self, domain):
        # Installed writers still require SQL admission in addition to the file
        # fence. This reservation touches no other repository or domain mutation.
        with reserved_write(self._store):
            yield

    @synchronized
    def workflow_publication(self):
        return legacy_journals.workflow_publication(self.conn)

    @synchronized
    def required(self, domain, path, owner=None):
        if domain == 'metadata-runtime':
            return self._metadata_required(owner)
        if domain == 'metadata-deliveries':
            return self.conn.execute('SELECT 1 FROM metadata_delivery_locations WHERE raw_filename=? LIMIT 1',
                                     (str(path),)).fetchone() is not None
        if domain == 'samples':
            return self.conn.execute("SELECT 1 FROM sample_attributes WHERE filename=? AND file_status='ready' LIMIT 1",
                                     (str(path),)).fetchone() is not None
        if domain == 'descriptions' and paid_description_access.retains(self.conn, path):
            return True
        return any(self.conn.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1',
                    (str(path),)).fetchone() is not None
                   for table, column in RELATIONS[domain])

    def _metadata_required(self, owner):
        if owner is None:
            return True
        if owner[0] == 'receipt':
            return metadata_file_writes.retains(self.conn, owner[1])
        kind, keys, revision = owner
        if kind == 'alternatives':
            row = self.conn.execute('SELECT s.at FROM enrichment_provider_state s JOIN enrichment_provider_config p '
                "ON p.provider=s.provider WHERE s.recording_id=? AND p.metadata_provider_id=? AND s.status<>'queued'", keys).fetchone()
        elif kind == 'conflicts':
            row = self.conn.execute('SELECT runtime_revision FROM metadata_conflicts WHERE runtime_id=?', keys).fetchone()
        else:
            table, key = {'native': ('rec_musical_sources', 'source_key'),
                          'choices': (runtime_metadata_choices.VIEW, 'id')}[kind]
            row = self.conn.execute(f'SELECT revision FROM {table} WHERE {key}=?', keys).fetchone()
        return row is not None and (1 <= revision <= row[0] if kind == 'choices' else row[0] == revision)

    def maintain_history(self, now):
        from yt_playlist.core import credit_retention, mode_retention
        progress = {}
        for domain, owner in (('credits', credit_retention), ('modes', mode_retention)):
            with atomic_write(self._store):
                progress[domain] = owner.step(self.conn, now, activate=False)
        return progress
