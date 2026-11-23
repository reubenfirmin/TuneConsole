"""SQL ownership and completion of pre-deletion playlist captures."""
from uuid import uuid4

from yt_playlist.core.playlist_backup_paths import record_id
from yt_playlist.repos.base import Repo, synchronized


class PlaylistBackupsRepo(Repo):
    @synchronized
    def reserve(self, payload, integrity):
        # Captures share the user's backup directory across databases. The SQL
        # capture ID is a UUID so separate libraries cannot overwrite each other.
        identity = uuid4().hex
        with self.conn.atomic():
            self.conn.execute('INSERT INTO collection_file_writes('
                'kind,owner_id,playlist_id,playlist_account_id,playlist_provider_id,playlist_captured_at,'
                'content_digest,byte_size,file_status) '
                "VALUES ('playlist-backup',?,?,?,?,?,?,?,'pending')", (identity, payload['playlist_id'], payload['identity_id'],
                payload['ytm_playlist_id'], payload['captured_at'], *integrity))
        return identity

    @synchronized
    def complete(self, identity, reference):
        if record_id(reference[0]) != identity:
            raise ValueError('Playlist backup filename does not match its reserved identity')
        with self.conn.atomic():
            changed = self.conn.execute("UPDATE collection_file_writes SET file_status='ready' "
                "WHERE kind='playlist-backup' AND owner_id=? AND content_digest=? AND byte_size=? AND file_status='pending'",
                (identity, *reference[1:])).rowcount
            if changed != 1:
                raise ValueError('Playlist backup reservation changed before completion')
