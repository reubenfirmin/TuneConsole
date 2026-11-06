"""Detached descriptive listening inputs. Only the analytics worker scans these rows."""
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_revisions import keyed_reads
from contextlib import nullcontext

from yt_playlist.core import listening_outputs
from yt_playlist.repos.base import Repo, lookup_read, synchronized, snapshot_read

BATCH_SIZE = 256


class ListeningAnalyticsRepo(Repo):
    def _batches(self, sql, args=()):
        if not getattr(self._store, '_read_only', False):
            raise RuntimeError('Listening analytics requires a private worker snapshot')
        with self._lock:
            check_cancellable_work()
            cursor = self.conn.execute(sql, args)
        try:
            while True:
                with self._lock:
                    check_cancellable_work()
                    rows = cursor.fetchmany(BATCH_SIZE)
                if not rows:
                    break
                yield [dict(row) for row in rows]
        finally:
            with self._lock:
                cursor.close()

    def catalog_rows(self):
        from yt_playlist.core import genre_classifications
        if not getattr(self._store, '_read_only', False):
            raise RuntimeError('Listening analytics requires a private worker snapshot')
        with self._lock:
            check_cancellable_work()
            genre = genre_classifications.read_expression(self.conn, 'r', 'recordings')
            artists = self.entry_artists_table
        return self._batches(f'SELECT r.identity_key,r.title,r.artist,{genre} AS genre,'
            f'(SELECT ta.artist_id FROM catalog_entries e JOIN {artists} ta ON ta.entry_id=e.id '
            'WHERE e.recording_id=r.id ORDER BY ta.position,e.id LIMIT 1) artist_id,'
            '(SELECT y.video_id FROM youtube_items y WHERE y.recording_id=r.id '
            'ORDER BY y.thumbnail IS NULL,y.video_id LIMIT 1) video_id,'
            '(SELECT y.thumbnail FROM youtube_items y WHERE y.recording_id=r.id '
            "AND y.thumbnail IS NOT NULL ORDER BY y.video_id LIMIT 1) thumbnail FROM recordings r")

    def rating_actions(self, now):
        return self._batches('SELECT a.*,r.identity_key FROM listening_rating_actions a '
            'LEFT JOIN youtube_items y ON y.video_id=a.video_id '
            'LEFT JOIN recordings r ON r.id=y.recording_id WHERE at<=? ORDER BY at,id', (now,))

    def impressions(self):
        return {r['item_key']: r['last_shown'] for batch in self._batches(
            "SELECT item_key,last_shown FROM rec_impressions WHERE surface='listening_editorial'") for r in batch}

    def plays(self, now):
        return self._batches('SELECT id,account_id,identity_key,video_id,playlist_ytm_id,played_at '
                            'FROM play_event_subjects WHERE played_at<=? ORDER BY played_at,id', (now,))

    def observations(self, now):
        # Retain dated source evidence, including generated contexts. Snapshot capture
        # times with unknown precision cannot establish a listening date.
        return self._batches("SELECT account_id,identity_key,day FROM listening_observation_subjects "
            "WHERE precision='calendar_day' AND day IS NOT NULL AND first_observed_at<=? "
            "GROUP BY account_id,identity_key,day ORDER BY day", (now,))

    def player(self, now, *, kinds):
        with keyed_reads(self._store, 'player_events', kinds):
            yield from self._batches('SELECT * FROM player_events WHERE kind IN (' +
                ','.join('?' for _ in kinds) + ') AND at<=? ORDER BY at,id', (*kinds, now))

    def feedback(self, now, *, kinds, positive, negative):
        return self._batches('SELECT item_key,CASE WHEN kind=? THEN ? ELSE ? END kind,MIN(created_at) at '
            'FROM rec_feedback WHERE kind IN (' + ','.join('?' for _ in kinds) + ') '
            'AND created_at<=? AND artist_id IS NULL GROUP BY item_key,CASE WHEN kind=? THEN ? ELSE ? END',
            (positive, positive, negative, *kinds, now, positive, positive, negative))

    def artist_names(self):
        return {str(row['id']): row['name'] for batch in self._batches('SELECT id,name FROM artists') for row in batch}

    def mix_playlist_ids(self):
        from yt_playlist.core import playlist_scope_state
        with self._lock:
            current = playlist_scope_state.available(self.conn)
        query = ("SELECT DISTINCT provider_playlist_id FROM playlist_scopes WHERE origin='tuneconsole'" if current else
                 "SELECT DISTINCT s.provider_playlist_id FROM playlist_provenance p JOIN playlist_scopes s ON s.id=p.ytm "
                 "WHERE p.origin='tuneconsole'")
        return {row['provider_playlist_id'] for batch in self._batches(query) for row in batch}

    def video_keys(self):
        return {row['video_id']: row['identity_key'] for batch in self._batches(
            'SELECT y.video_id,r.identity_key FROM youtube_items y JOIN recordings r ON r.id=y.recording_id')
            for row in batch}

    @lookup_read
    def uses_files(self):
        return listening_outputs.available(self.conn)

    @synchronized
    def publish_files(self, publication, durable, *, keep_previous, timezone=None):
        with nullcontext() if self.conn.in_transaction else self.conn.atomic():
            identity = listening_outputs.register(self.conn, publication, durable, keep_previous=keep_previous)
            if timezone is not None:
                self.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('listening_timezone',?)", (timezone,))
            return identity

    @synchronized
    def publish_legacy(self, documents, *, retained_surfaces=None, timezone=None):
        with nullcontext() if self.conn.in_transaction else self.conn.atomic():
            self.conn.executemany('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at', documents)
            if retained_surfaces:
                self.conn.execute("DELETE FROM rec_proposals WHERE surface LIKE 'listening_chunk:%' "
                    'AND surface NOT IN (' + ','.join('?' for _ in retained_surfaces) + ')', retained_surfaces)
            elif retained_surfaces is not None:
                self.conn.execute("DELETE FROM rec_proposals WHERE surface LIKE 'listening_chunk:%'")
            if timezone is not None:
                self.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('listening_timezone',?)", (timezone,))

    @snapshot_read
    def output(self, surface, file_surface, chunk_key):
        from yt_playlist.core.preparation import listening_publication
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            owner = listening_publication.OWNER
            identity = owner.selected(self.conn)
            if identity is not None:
                header = owner.capture(self.conn, identity, surface)
                return {'source': 'prepared', 'header': dict(header) if header is not None else None}
        if self._store._listening_files:
            row = self.conn.execute('SELECT f.filename,f.content_digest,f.byte_size FROM listening_output_heads h '
                'JOIN listening_output_files f ON f.publication_id=h.publication_id '
                'WHERE f.surface=? AND f.chunk_key=? ' +
                ("AND h.slot='current' " if not chunk_key else '') + 'ORDER BY h.slot LIMIT 1',
                (file_surface, chunk_key)).fetchone()
            return {'source': 'file', 'reference': tuple(row) if row is not None else None}
        row = self.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        return {'source': 'legacy', 'raw': row[0] if row else None}

    @synchronized
    def record_impressions(self, signatures, now, *, last_shown_before):
        with self.conn:
            self.conn.executemany("INSERT INTO rec_impressions(surface,item_key,views,last_shown) "
                "VALUES ('listening_editorial',?,1,?) ON CONFLICT(surface,item_key) DO UPDATE "
                'SET views=rec_impressions.views+1,last_shown=excluded.last_shown '
                'WHERE rec_impressions.last_shown<?',
                [(signature, now, last_shown_before) for signature in signatures])
