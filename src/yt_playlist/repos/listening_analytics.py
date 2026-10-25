"""Detached descriptive listening inputs. Only the analytics worker scans these rows."""
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.repos.base import Repo, synchronized

BATCH_SIZE = 256
PLAYER_KINDS = ('listen_start', 'tick', 'state', 'volume', 'track_exit', 'ended', 'bye')


class ListeningAnalyticsRepo(Repo):
    def batches(self, sql, args=()):
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
        return self.batches('SELECT r.identity_key,r.title,r.artist,r.genre,'
            '(SELECT ta.artist_id FROM catalog_entries e JOIN track_artists ta ON ta.entry_id=e.id '
            'WHERE e.recording_id=r.id ORDER BY ta.position,e.id LIMIT 1) artist_id,'
            '(SELECT y.thumbnail FROM youtube_items y WHERE y.recording_id=r.id '
            "AND y.thumbnail IS NOT NULL ORDER BY y.video_id LIMIT 1) thumbnail FROM recordings r")

    def rating_actions(self, now):
        return self.batches('SELECT a.*,r.identity_key FROM listening_rating_actions a '
            'LEFT JOIN youtube_items y ON y.video_id=a.video_id '
            'LEFT JOIN recordings r ON r.id=y.recording_id WHERE at<=? ORDER BY at,id', (now,))

    def impressions(self):
        return {r['item_key']: r['last_shown'] for batch in self.batches(
            "SELECT item_key,last_shown FROM rec_impressions WHERE surface='listening_editorial'") for r in batch}

    def plays(self, now):
        return self.batches('SELECT id,identity_id,identity_key,video_id,playlist_ytm_id,played_at '
                            'FROM play_events WHERE played_at<=? ORDER BY played_at,id', (now,))

    def observations(self, now):
        # Retain dated source evidence, including generated contexts. Snapshot capture
        # times with unknown precision cannot establish a listening date.
        return self.batches("SELECT identity_id,identity_key,day FROM listening_observations "
            "WHERE precision='calendar_day' AND day IS NOT NULL AND first_observed_at<=? "
            "GROUP BY identity_id,identity_key,day ORDER BY day", (now,))

    def player(self, now):
        with keyed_reads(self._store, 'player_events', PLAYER_KINDS):
            yield from self.batches('SELECT * FROM player_events WHERE kind IN (' +
                ','.join('?' for _ in PLAYER_KINDS) + ') AND at<=? ORDER BY at,id', (*PLAYER_KINDS, now))

    def feedback(self, now):
        return self.batches("SELECT item_key,kind,MIN(created_at) at FROM rec_feedback "
            "WHERE kind IN ('like','dislike','ban') AND created_at<=? AND artist_id IS NULL "
            "GROUP BY item_key,kind", (now,))

    @synchronized
    def encoded(self, surface='listening_analytics'):
        row = self.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        return row[0] if row else None

    def prepared(self, surface='listening_analytics'):
        import json
        encoded = self.encoded(surface)
        return json.loads(encoded) if encoded else None

    @synchronized
    def record_impressions(self, signatures, now):
        with self.conn:
            self.conn.executemany("INSERT INTO rec_impressions(surface,item_key,views,last_shown) "
                "VALUES ('listening_editorial',?,1,?) ON CONFLICT(surface,item_key) DO UPDATE "
                'SET views=rec_impressions.views+1,last_shown=excluded.last_shown '
                'WHERE rec_impressions.last_shown<excluded.last_shown-21600',
                [(signature, now) for signature in signatures])

    @synchronized
    def publish(self, encoded, now, surface='listening_analytics'):
        """Participate in ReadSnapshot.publish's guarded transaction; no commit here."""
        self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
            'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
            (surface, encoded, now))
