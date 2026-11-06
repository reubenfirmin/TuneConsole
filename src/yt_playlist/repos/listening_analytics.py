"""Detached descriptive listening inputs. Only the analytics worker scans these rows."""
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_revisions import keyed_reads
from contextlib import nullcontext
from dataclasses import dataclass

from yt_playlist.core import listening_outputs
from yt_playlist.repos.base import Repo, synchronized, snapshot_read

BATCH_SIZE = 256
PLAYER_KINDS = ('listen_start', 'tick', 'state', 'volume', 'track_exit', 'ended', 'bye')


@dataclass(frozen=True)
class ListeningGeneration:
    publication: object
    chunks: dict
    manifest: str
    edition: str
    retained_chunks: tuple
    now: float


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

    def player(self, now):
        with keyed_reads(self._store, 'player_events', PLAYER_KINDS):
            yield from self._batches('SELECT * FROM player_events WHERE kind IN (' +
                ','.join('?' for _ in PLAYER_KINDS) + ') AND at<=? ORDER BY at,id', (*PLAYER_KINDS, now))

    def feedback(self, now):
        return self._batches("SELECT item_key,CASE WHEN kind='like' THEN 'like' ELSE 'dislike' END kind,MIN(created_at) at FROM rec_feedback "
            "WHERE kind IN ('like','dislike','ban') AND created_at<=? AND artist_id IS NULL "
            "GROUP BY item_key,CASE WHEN kind='like' THEN 'like' ELSE 'dislike' END", (now,))

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

    def prepare_generation(self, chunks, manifest, edition, now, *, input_revision):
        """Prepare immutable files or retain the predecessor's chunk identities."""
        if self._store._listening_files:
            publication = listening_outputs.prepare(chunks, manifest, edition, now, input_revision=input_revision)
            retained = ()
        else:
            publication = None
            retained = tuple(item['key'] for item in (self.prepared() or {}).get('chunks', []))
        return ListeningGeneration(publication, chunks, manifest, edition, retained, now)

    @synchronized
    def publish_generation(self, generation, timezone):
        """Publish and retire together inside the snapshot's guarded transaction."""
        if not self.conn.in_transaction:
            raise ValueError('Listening generation requires the guarded outer transaction')
        if generation.publication is not None:
            listening_outputs.publish(self.conn, generation.publication)
        else:
            for key, encoded in generation.chunks.items():
                self.publish(encoded, generation.now, 'listening_chunk:' + key)
            self.publish(generation.edition, generation.now, 'listening_edition')
            self.publish(generation.manifest, generation.now)
            active = tuple('listening_chunk:' + key for key in {*generation.chunks, *generation.retained_chunks})
            if active:
                self.conn.execute("DELETE FROM rec_proposals WHERE surface LIKE 'listening_chunk:%' "
                    'AND surface NOT IN (' + ','.join('?' for _ in active) + ')', active)
            else:
                self.conn.execute("DELETE FROM rec_proposals WHERE surface LIKE 'listening_chunk:%'")
        self.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('listening_timezone',?)", (timezone,))

    @snapshot_read
    def encoded(self, surface='listening_analytics'):
        from yt_playlist.core.preparation import listening_publication
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            handled, encoded = listening_publication.read(self.conn, surface)
            if handled:
                return encoded
        if self._store._listening_files:
            return listening_outputs.encoded(self.conn, surface)
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
        if self._store._listening_files:
            with nullcontext() if self.conn.in_transaction else self.conn.atomic():
                listening_outputs.publish_document(self.conn, surface, encoded, now)
            return
        self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
            'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
            (surface, encoded, now))
