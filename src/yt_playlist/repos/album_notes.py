"""SQL references and revision guards for editorial demand and note output."""
from contextlib import contextmanager

from yt_playlist.core import album_note_outputs, album_note_requests
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized


class AlbumNotesRepo(Repo):
    @lookup_read
    def native_outputs(self):
        return self._store._album_note_demand

    @snapshot_read
    def output_document(self):
        with keyed_reads(self._store, 'rec_proposals', ('album_notes',)):
            if self._store._album_note_demand:
                return None, [dict(row) for row in self.conn.execute(
                    'SELECT browse_id,filename,content_digest,byte_size FROM album_note_outputs ORDER BY id')]
            from yt_playlist.core.preparation import album_note_publication
            revision = album_note_publication.selected(self.conn)
            if revision is not None:
                return None, [dict(row) for row in self.conn.execute(
                    'SELECT browse_id,filename,digest AS content_digest,byte_size FROM ' +
                    album_note_publication.preparation.FILES + ' WHERE generation=? ORDER BY position', (revision,))]
            row = self.conn.execute("SELECT payload FROM rec_proposals WHERE surface='album_notes'").fetchone()
            return row[0] if row else None, None

    @lookup_read
    def outputs_present(self):
        with keyed_reads(self._store, 'rec_proposals', ('album_notes',)):
            if self._store._album_note_demand:
                return self.conn.execute('SELECT 1 FROM album_note_outputs LIMIT 1').fetchone() is not None
            from yt_playlist.core.preparation import album_note_publication
            revision = album_note_publication.selected(self.conn)
            if revision is not None:
                return self.conn.execute('SELECT 1 FROM ' + album_note_publication.preparation.FILES +
                    ' WHERE generation=? LIMIT 1', (revision,)).fetchone() is not None
            return self.conn.execute("SELECT 1 FROM rec_proposals WHERE surface='album_notes' "
                                     "AND payload NOT IN ('null','{}')").fetchone() is not None

    @synchronized
    def replace_outputs(self, durable, now):
        with self.conn.atomic(join=True):
            self.conn.execute('DELETE FROM album_note_outputs')
            for browse, reference in durable:
                album_note_outputs._insert(self.conn, browse, *reference, now)

    @contextmanager
    def transaction(self):
        with self._lock, self.conn.atomic(join=True):
            yield

    @snapshot_read
    def requests(self):
        return album_note_requests.read(self.conn)

    @lookup_read
    def next_request(self):
        return album_note_requests.next_request(self.conn)

    @lookup_read
    def matches(self, browse, expected):
        return album_note_requests.matches(self.conn, browse, expected)

    @synchronized
    def remove_request(self, browse, expected=None):
        album_note_requests.complete(self.conn, browse, expected)

    @synchronized
    def store_request(self, fields, now, *, changed_work):
        browse = fields[0]
        previous = self.conn.execute('SELECT * FROM album_note_requests WHERE browse_id=?', (browse,)).fetchone()
        if previous is None:
            album_note_requests._insert(self.conn, fields, now)
            return
        reference, reason = album_note_requests._reference(self.conn, browse)
        current = album_note_requests._album(previous)
        old_fields = album_note_requests.parse({browse: current})[0]
        if old_fields == fields and previous['album_provider_id'] == reference:
            return
        changed_work = changed_work or previous['album_provider_id'] != reference
        fence = ',work_revision=work_revision+?' if 'work_revision' in previous.keys() else ''
        values = (reference, reason, *fields[1:], *([int(changed_work)] if fence else []), now, previous['id'])
        self.conn.execute('UPDATE album_note_requests SET album_provider_id=?,unresolved_reason=?,artist=?,title=?,'
            'year_text=?,year_number=?,thumbnail=?,requested_until=?,revision=revision+1' + fence + ',updated_at=? '
            'WHERE id=?', values)

    @synchronized
    def replace_output(self, browse, reference, now, limit):
        self.conn.execute('DELETE FROM album_note_outputs WHERE browse_id=?', (browse,))
        self.conn.execute('DELETE FROM album_note_outputs WHERE id IN '
            '(SELECT id FROM album_note_outputs ORDER BY id DESC LIMIT -1 OFFSET ?)', (limit - 1,))
        album_note_outputs._insert(self.conn, browse, *reference, now)
