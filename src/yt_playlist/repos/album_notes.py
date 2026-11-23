"""Editorial demand and guarded note publication."""
from contextlib import contextmanager

from yt_playlist.core import album_note_outputs, album_note_requests
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized


class AlbumNotesRepo(Repo):
    @snapshot_read
    def output_document(self):
        return album_note_outputs.read(self.conn)

    @lookup_read
    def outputs_present(self):
        return self.conn.execute('SELECT 1 FROM album_provider_ids WHERE note_json IS NOT NULL LIMIT 1').fetchone() is not None

    @synchronized
    def replace_outputs(self, prepared):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE album_provider_ids SET note_json=NULL WHERE note_json IS NOT NULL')
            for browse, raw in prepared:
                album_note_outputs.store(self.conn, browse, raw)

    @synchronized
    def publish_output(self, browse, raw, *, expected_request=None):
        with self.conn.atomic():
            self.conn.execute('UPDATE album_provider_ids SET note_json=note_json WHERE 0')
            if expected_request is not None and not album_note_requests.matches(self.conn, browse, expected_request):
                return False
            album_note_outputs.store(self.conn, browse, raw)
            self.conn.execute('UPDATE album_provider_ids SET note_json=NULL WHERE provider=\'youtube\' '
                'AND provider_id IN (SELECT provider_id FROM album_provider_ids WHERE note_json IS NOT NULL '
                'ORDER BY note_output_order DESC LIMIT -1 OFFSET ?)', (album_note_outputs.MAX_NOTES,))
            album_note_requests.complete(self.conn, browse, expected_request)
            return True

    @contextmanager
    def transaction(self):
        with self._lock, self.conn.atomic(join=True):
            self.conn.execute('UPDATE album_provider_ids SET note_json=note_json WHERE 0')
            yield

    @snapshot_read
    def requests(self):
        return album_note_requests.read(self.conn)

    @lookup_read
    def next_request(self):
        return album_note_requests.next_request(self.conn)

    @synchronized
    def remove_request(self, browse):
        album_note_requests.complete(self.conn, browse)

    @synchronized
    def store_request(self, fields):
        album_note_requests.store(self.conn, fields)
