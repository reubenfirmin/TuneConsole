"""SQL ownership and completion of pre-deletion playlist captures."""
from uuid import uuid4

from yt_playlist.core.playlist_backup_paths import record_id
from yt_playlist.core.schema_contract import MigrationError, tokens
from yt_playlist.repos.base import Repo, synchronized

SQL = '''CREATE TABLE playlist_backup_writes (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    playlist_id INTEGER,
    account_id INTEGER NOT NULL,
    provider_playlist_id TEXT NOT NULL CHECK(length(provider_playlist_id)>0),
    captured_at REAL NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 67108864),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''


def install(conn, context=None):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='playlist_backup_writes'").fetchone()
    if row is None:
        conn.execute(SQL)
    elif tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible playlist backup file identities')


def validate(conn):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='playlist_backup_writes'").fetchone()
    if row is None or tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible playlist backup file identities')


class PlaylistBackupsRepo(Repo):
    @synchronized
    def reserve(self, payload, integrity):
        # Captures share the user's backup directory across databases. The SQL
        # primary key is a UUID so separate libraries cannot overwrite each other.
        identity = uuid4().hex
        with self.conn.atomic():
            install(self.conn)
            self.conn.execute('INSERT INTO playlist_backup_writes('
                'id,playlist_id,account_id,provider_playlist_id,captured_at,content_digest,byte_size,file_status) '
                "VALUES (?,?,?,?,?,?,?,'pending')", (identity, payload['playlist_id'], payload['identity_id'],
                payload['ytm_playlist_id'], payload['captured_at'], *integrity))
        return identity

    @synchronized
    def complete(self, identity, reference):
        if record_id(reference[0]) != identity:
            raise ValueError('Playlist backup filename does not match its reserved identity')
        with self.conn.atomic():
            changed = self.conn.execute("UPDATE playlist_backup_writes SET file_status='ready' "
                "WHERE id=? AND content_digest=? AND byte_size=? AND file_status='pending'",
                (identity, *reference[1:])).rowcount
            if changed != 1:
                raise ValueError('Playlist backup reservation changed before completion')
