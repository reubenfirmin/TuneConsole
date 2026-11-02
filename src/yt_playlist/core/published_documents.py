"""Read retained selected documents without loading their preparation workers."""
from dataclasses import dataclass
import hashlib

from . import recommendation_outputs as files
from .schema_contract import MigrationError


@dataclass(frozen=True)
class Document:
    source: str
    prefix: str
    native_table: str
    surface: str | None = None
    nullable: bool = False

    def selected(self, conn):
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.native_table,)).fetchone():
            return None
        head = self.prefix.replace('prepared_', 'published_', 1) + '_head'
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (head,)).fetchone():
            return None
        row = conn.execute('SELECT revision FROM '+head+' WHERE name=? AND active=1', (self.source,)).fetchone()
        return row[0] if row else None

    def value(self, conn, revision, default=None):
        generations = self.prefix.replace('prepared_', 'published_', 1) + '_generations'
        row = conn.execute('SELECT generation FROM '+generations+' WHERE revision=?', (revision,)).fetchone()
        if row is None:
            raise MigrationError('Published document generation is missing')
        if row[0] is None:
            return default
        captured = conn.execute('SELECT * FROM '+self.prefix+'_generations WHERE source=? AND generation=?',
                                (self.source, row[0])).fetchone()
        if captured is None:
            raise MigrationError('Published document original is missing')
        return self.read_original(conn, captured)

    def read_original(self, conn, row):
        parts = conn.execute('SELECT position,filename,digest,byte_size FROM '+self.prefix+'_original_parts '
            'WHERE source=? AND generation=? ORDER BY position LIMIT 257',
            (self.source, row['generation'])).fetchall()
        if [part[0] for part in parts] != list(range(row['part_count'])):
            raise MigrationError('Prepared document original parts are incomplete')
        raw = ''.join(files.read(*part[1:], surface=self.surface or self.source, chunk_key=str(part[0])) for part in parts)
        if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
            raise MigrationError('Prepared document differs from its original digest')
        return None if self.nullable and row['source_kind'] == 'null' else raw


ALBUM_FEEDBACK = Document('album_feedback', 'prepared_album_feedback', 'album_feedback')
ACCOUNT_AUTH = Document('auth_expired_identities', 'prepared_account_auth', 'account_auth_expirations')
PLAYBACK_BINDINGS = Document('listening_playback_bindings', 'prepared_playback_binding', 'listening_playback_bindings')
PENDING_ALBUM = Document('listening_album_pending', 'prepared_album_playback', 'album_playback_intents')
LIBRARY_SYNC = Document('library_sync_state', 'prepared_library_sync', 'sync_attempts', 'library_sync', True)
