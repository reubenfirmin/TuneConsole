"""Canonical album rows, exact release links and their ordered artist references."""
from contextlib import contextmanager

from yt_playlist.core import album_tracklists, artist_credits, genre_classifications
from yt_playlist.repos.base import Repo, synchronized, snapshot_read


class AlbumRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock, self.conn.atomic(join=True):
            self.conn.execute('UPDATE albums SET title=title WHERE 0')
            yield

    @synchronized
    def observation_inputs(self, browse):
        row = self.conn.execute("SELECT a.* FROM albums a JOIN album_provider_ids p ON p.album_id=a.id "
            "WHERE p.provider='youtube' AND p.provider_id=?", (browse,)).fetchone()
        aid = row['id'] if row else None
        return {'album': dict(row) if row else {}, 'keyed_genres': genre_classifications.available(self.conn),
                'genre': genre_classifications.read(self.conn, 'albums', aid) if aid is not None else None,
                'credits': [dict(credit) for credit in self.conn.execute(
                    'SELECT * FROM album_artists WHERE album_id=? ORDER BY position', (aid,))]}

    @synchronized
    def store_metadata(self, browse, aid, values):
        if aid is None:
            aid = self.conn.execute('INSERT INTO albums('+','.join(values)+') VALUES ('+
                ','.join('?' for _ in values)+')', tuple(values.values())).lastrowid
            self.conn.execute("INSERT INTO album_provider_ids(provider,provider_id,album_id) VALUES ('youtube',?,?)", (browse, aid))
        else:
            self.conn.execute('UPDATE albums SET '+','.join(field+'=?' for field in values)+' WHERE id=?',
                              (*values.values(), aid))
        return aid

    @synchronized
    def store_credits(self, aid, credits):
        with self.conn.atomic(join=True):
            self.conn.execute('DELETE FROM album_artists WHERE album_id=?', (aid,))
            self.conn.executemany('INSERT INTO album_artists VALUES (?,?,?,?)',
                [(aid, position, artist, credit) for position, (artist, credit) in enumerate(credits)])

    @synchronized
    def link_entry(self, aid, entry, source):
        with self.conn.atomic(join=True):
            if album_tracklists.available(self.conn):
                album_tracklists.associate(self.conn, aid, entry, source)
            elif not self.conn.execute('SELECT 1 FROM album_tracks WHERE album_id=? AND entry_id=?', (aid, entry)).fetchone():
                self.conn.execute('INSERT INTO album_tracks(album_id,entry_id,is_full,source) VALUES (?,?,0,?)', (aid, entry, source))

    @snapshot_read
    def linked_artist_inputs(self, aid):
        return {'credits': [dict(row) for row in self.conn.execute(
            'SELECT aa.artist_id,aa.credit,a.provisional FROM album_artists aa JOIN artists a ON a.id=aa.artist_id '
            'WHERE aa.album_id=?', (aid,))],
            'subjects': [row[0] for row in self.conn.execute('SELECT DISTINCT ta.artist_id FROM album_tracks at '
                f'JOIN {artist_credits.projection_table(self.conn)} ta ON ta.entry_id=at.entry_id '
                'JOIN artist_provider_ids p ON p.artist_id=ta.artist_id WHERE at.album_id=?', (aid,))]}

    @synchronized
    def rebind_artist(self, aid, subject):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE album_artists SET artist_id=? WHERE album_id=?', (subject, aid))
