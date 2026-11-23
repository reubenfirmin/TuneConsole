"""Detached descriptive listening inputs. Only the analytics worker scans these rows."""
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_revisions import keyed_reads
from contextlib import nullcontext

from yt_playlist.core import listening_outputs, rating_actions, recommendation_impressions
from yt_playlist.repos.base import Repo, synchronized, snapshot_read
from . import recommendation_file_writes

BATCH_SIZE = 256


class ListeningAnalyticsRepo(Repo):
    @synchronized
    def reserve_file(self, surface, chunk_key, integrity):
        with self.conn.atomic():
            return recommendation_file_writes.reserve(self.conn, 'listening_output_files', surface, chunk_key, integrity)

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
            f'(SELECT ta.artist_id FROM track_sources e JOIN {artists} ta ON ta.entry_id=e.id '
            'WHERE e.recording_id=r.id ORDER BY ta.position,e.id LIMIT 1) artist_id,'
            '(SELECT y.video_id FROM track_sources y WHERE y.recording_id=r.id AND y.video_id IS NOT NULL '
            'ORDER BY y.thumbnail IS NULL,y.video_id LIMIT 1) video_id,'
            '(SELECT y.thumbnail FROM track_sources y WHERE y.recording_id=r.id AND y.video_id IS NOT NULL '
            "AND y.thumbnail IS NOT NULL ORDER BY y.video_id LIMIT 1) thumbnail FROM recordings r")

    def rating_actions(self, now):
        with keyed_reads(self._store, 'player_events', (rating_actions.KIND,)):
            yield from self._batches(f'SELECT a.*,r.identity_key FROM {rating_actions.VIEW} a '
                'LEFT JOIN track_sources y ON y.video_id=a.video_id '
                'LEFT JOIN recordings r ON r.id=y.recording_id WHERE at<=? ORDER BY at,id', (now,))

    @snapshot_read
    def impressions(self):
        values = recommendation_impressions.counts(self.conn, recommendation_impressions.EDITORIAL)
        return {signature: at for signature, (_, at) in values.items()}

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
        query = "SELECT DISTINCT provider_playlist_id FROM playlist_scopes WHERE origin='tuneconsole'"
        return {row['provider_playlist_id'] for batch in self._batches(query) for row in batch}

    def video_keys(self):
        return {row['video_id']: row['identity_key'] for batch in self._batches(
            'SELECT y.video_id,r.identity_key FROM track_sources y JOIN recordings r ON r.id=y.recording_id WHERE y.video_id IS NOT NULL')
            for row in batch}

    @synchronized
    def publish_files(self, publication, durable, *, keep_previous, timezone=None, file_ids):
        with nullcontext() if self.conn.in_transaction else self.conn.atomic():
            if len(file_ids) != len(durable):
                raise ValueError('Listening files require their pending SQL identities')
            for identity, (surface, key, filename, digest, size, _) in zip(file_ids, durable):
                recommendation_file_writes.complete(self.conn, identity, 'listening_output_files', surface, key,
                                                     (filename, digest, size))
            identity = listening_outputs.register(self.conn, publication, durable, keep_previous=keep_previous)
            if timezone is not None:
                self.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('listening_timezone',?)", (timezone,))
            return identity

    @snapshot_read
    def output_reference(self, surface, chunk_key):
        row = listening_outputs.reference(self.conn, surface, chunk_key)
        return tuple(row) if row is not None else None

    @synchronized
    def record_impressions(self, signatures, now, *, last_shown_before):
        with self.conn.atomic(join=True):
            recommendation_impressions.advance(self.conn, recommendation_impressions.EDITORIAL, signatures, now,
                                              eligible=lambda at: at < last_shown_before)
