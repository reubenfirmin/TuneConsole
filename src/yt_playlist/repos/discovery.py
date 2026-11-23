"""DiscoveryRepo: the background outward-discovery state: which artists we've scanned (and when),
and the accumulating pools of new albums / new artists we've found, with last-shown bookkeeping.

Tables are created by numbered migrations. The interest signal (which artists are worth
scanning) is computed live from your library: play counts, playlist participation, saved albums.
"""
from contextlib import contextmanager, ExitStack
import json
from yt_playlist.core.catalog_query import LIBRARY_RECORDINGS_SQL, LIBRARY_TRACKS_SQL, is_playable_sql, library_recordings_sql
from yt_playlist.core import album_aliases, discovery_provenance as provenance
from yt_playlist.core import discovery_candidates as candidates
from yt_playlist.core.artist_reconciliation import move_image_retry, move_scan_time
from yt_playlist.core.db_revisions import column_group_reads

from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.core import artist_keys, genre_classifications

# Audio/content columns persisted per discovered track (mirrors the library `tracks` audio set).
_DTRACK_AUDIO = ("bpm", "danceability", "mood_happy", "mood_sad", "mood_relaxed",
                 "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
                 "music_key", "music_scale")
_DTRACK_FIELDS = ("id", "recording_id", "identity_key", "video_id", "title", "artist", "album", "thumbnail",
                  "genre", "year", "source_browse_id", "found_at", "first_shown", "last_shown",
                  "offered_count", "last_enriched")
_DTRACK_PROJECTION = ','.join((*_DTRACK_FIELDS, *_DTRACK_AUDIO))

_GENERATED_PL = "(SELECT playlist_scope_id FROM quarantined_playlists)"
_STARTER_ORIGINS = "(SELECT id FROM discovery_origins WHERE kind='artist_start')"


class DiscoveryRepo(Repo):
    @synchronized
    def reserve_explanation_file(self, artist_id, integrity):
        from yt_playlist.repos import collection_file_writes
        with self.conn.atomic():
            return collection_file_writes.reserve(self.conn, 'discovery-candidate', artist_id, integrity)

    @snapshot_read
    def track_reassociation_inputs(self, video, previous, target, *, merge_recordings):
        member = candidates.track(self.conn, previous)
        sources = [dict(row) for row in self.conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? ' +
            ('' if merge_recordings else 'AND input_video_id=?'), (previous,) if merge_recordings else (previous, video))]
        return dict(previous=previous, target=target, video=video, merge_recordings=merge_recordings,
                    member=dict(member) if member else None, sources=sources)

    @synchronized
    def finish_track_reassociation(self, inputs, *, retain_member, membership_found, association=None):
        previous, target, video = inputs['previous'], inputs['target'], inputs['video']
        with self.conn.atomic(join=True):
            merge_recordings = inputs.get('merge_recordings', inputs.get('remaining') is None)
            if retain_member:
                entry = self.conn.execute('SELECT id FROM track_sources WHERE video_id=?', (video,)).fetchone()[0]
                candidates.admit_track(self.conn, target, entry, membership_found, merge=True)
            for row in inputs['sources']:
                provenance.retain(self.conn, target, row['origin_id'], row['input_video_id'],
                    row['last_found_at'], first_found_at=row['first_found_at'])
                self.conn.execute('DELETE FROM discovery_sources WHERE id=?', (row['id'],))
            if merge_recordings:
                provenance.merge_subject(self.conn, 'track', previous, target)
            else:
                provenance.refresh_counts(self.conn, 'track', target)
            from yt_playlist.core.association_work import complete
            complete(self.conn, association, 'discovery')


    @snapshot_read
    def artist_reconciliation_inputs(self, previous, target):
        return {
            'discovered_artists': [dict(row) for row in self.conn.execute(
                'SELECT * FROM discovered_artists WHERE artist_id IN (?,?)', (previous, target))],
            'sources': [dict(row) for row in self.conn.execute(
                'SELECT * FROM discovery_artist_sources WHERE artist_id=?', (previous,))],
        }

    @snapshot_read
    def origin_merge_inputs(self, column, previous, target):
        if column not in ('artist_id', 'album_id'):
            raise ValueError('Unsupported discovery origin identity')
        inputs = []
        for row in self.conn.execute('SELECT * FROM discovery_origins WHERE '+column+'=?', (previous,)):
            current = self.conn.execute('SELECT id FROM discovery_origins WHERE kind=? AND '+column+'=?',
                                        (row['kind'], target)).fetchone()
            inputs.append({'origin': dict(row), 'target': current[0] if current else None,
                'sources': [dict(source) for source in self.conn.execute(
                    'SELECT * FROM discovery_sources WHERE origin_id=?', (row['id'],))] if current else [],
                'artists': [dict(source) for source in self.conn.execute(
                    'SELECT * FROM discovery_artist_sources WHERE origin_id=?', (row['id'],))]})
        return inputs

    def _move_origins(self, column, target, origins):
        for item in origins:
            old, current = item['origin']['id'], item['target']
            if current is None:
                self.conn.execute('UPDATE discovery_origins SET '+column+'=? WHERE id=?', (target, old))
            else:
                provenance.merge_fetches(self.conn, old, current)
                for row in item['sources']:
                    provenance.retain(self.conn, row['recording_id'], current, row['input_video_id'],
                        row['last_found_at'], first_found_at=row['first_found_at'])
                for row in item['artists']:
                    provenance.retain_artist(self.conn, row['artist_id'], current, row['score'])
                self.conn.execute('DELETE FROM discovery_sources WHERE origin_id=?', (old,))
                self.conn.execute('DELETE FROM discovery_artist_sources WHERE origin_id=?', (old,))
                self.conn.execute('DELETE FROM discovery_origins WHERE id=?', (old,))
            for row in item['artists']:
                provenance.invalidate_explanation(self.conn, row['artist_id'])

    @synchronized
    def apply_artist_reconciliation(self, previous, target, inputs, origins, candidate):
        with self.conn.atomic(join=True):
            for row in inputs['sources']:
                provenance.retain_artist(self.conn, target, row['origin_id'], row['score'])
            self.conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=?', (previous,))
            self._move_origins('artist_id', target, origins)
            move_image_retry(self.conn, previous, target)
            move_scan_time(self.conn, previous, target)
            if candidate is not None:
                candidates.merge_artist(self.conn, previous, target, candidate)
            provenance.merge_subject(self.conn, 'artist', previous, target)
            provenance.invalidate_explanation(self.conn, target)

    @snapshot_read
    def album_merge_inputs(self, previous, target):
        return [dict(row) for row in self.conn.execute(
            'SELECT discovery_found_at AS found_at FROM albums WHERE id IN (?,?) '
            'AND discovery_found_at IS NOT NULL', (previous, target))]

    @synchronized
    def apply_album_merge(self, previous, target, origins, member):
        with self.conn.atomic():
            self.conn.execute('UPDATE albums SET discovery_found_at=NULL WHERE id=?', (previous,))
            self.conn.execute('UPDATE albums SET discovery_found_at=? WHERE id=?', (member, target))
            self._move_origins('album_id', target, origins)
            provenance.merge_subject(self.conn, 'album', previous, target)

    @synchronized
    def remove_spotify_origins(self):
        with self.conn.atomic():
            return provenance.remove_spotify_artists(self.conn)

    @lookup_read
    def fetched_albums(self, browse_ids):
        """Detach retained fetch checkpoints without borrowing another writer's transaction."""
        from yt_playlist.core import discovery_checkpoints
        return discovery_checkpoints.fetched(self.conn, browse_ids)

    @synchronized
    def remember_album_fetch(self, browse):
        from yt_playlist.core import discovery_checkpoints
        with self.conn.atomic():
            discovery_checkpoints.remember(self.conn, (browse,))

    @snapshot_read
    def starter_artists(self):
        """Explicit artist choices, independent of imported plays, likes and ownership."""
        return [dict(row) for row in self.conn.execute(
            'SELECT a.id artist_id,a.name name,p.provider_id browse_id,d.thumbnail '
            'FROM main.discovery_artist_sources s JOIN artists a ON a.id=s.artist_id '
            'JOIN main.discovery_origins o ON o.id=s.origin_id '
            "JOIN artist_provider_ids p ON p.artist_id=a.id AND p.provider='youtube' "
            'LEFT JOIN discovered_artists d ON d.artist_id=a.id '
            "WHERE o.kind='artist_start' AND o.artist_id=a.id ORDER BY a.id,p.provider_id")]

    @synchronized
    def select_starter_song(self, video_id, now):
        with self.conn:
            self.conn.execute('UPDATE track_sources SET starter_selected_at=? '
                              'WHERE video_id=? AND starter_selected_at IS NULL', (now, video_id))

    @snapshot_read
    def starter_songs(self):
        return [dict(row) for row in self.conn.execute(
            'SELECT t.identity_key AS key,t.identity_key,t.video_id,t.title,t.artist,t.album,t.thumbnail,t.duration_s '
            'FROM track_sources s JOIN catalog_tracks t ON t.id=s.id '
            'WHERE s.starter_selected_at IS NOT NULL AND COALESCE(t.available,1)<>0 '
            'ORDER BY s.starter_selected_at,s.video_id')]

    @snapshot_read
    def starter_song_videos(self):
        return [row[0] for row in self.conn.execute(
            'SELECT video_id FROM track_sources WHERE starter_selected_at IS NOT NULL')]

    @lookup_read
    def starter_count(self, excluding):
        return self.conn.execute('SELECT COUNT(DISTINCT artist_id) FROM discovery_artist_sources '
                                 f'WHERE origin_id IN {_STARTER_ORIGINS} AND artist_id<>?', (excluding,)).fetchone()[0]

    @synchronized
    def set_starter_artist(self, aid, thumbnail, selected, now, *, score, maximum, origin):
        with self.conn:
            if selected:
                count = self.conn.execute("SELECT COUNT(DISTINCT s.artist_id) FROM discovery_artist_sources s "
                    f'WHERE s.origin_id IN {_STARTER_ORIGINS} AND s.artist_id<>?', (aid,)).fetchone()[0]
                if count >= maximum:
                    raise ValueError('Choose up to 12 artists to start.')
                self.conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=? '
                                  f'AND origin_id IN {_STARTER_ORIGINS}', (aid,))
                self._store_artist_source(aid, score, origin)
                candidates.admit_artist(self.conn, aid, score, thumbnail, now)
                provenance.refresh_counts(self.conn,'artist',aid)
            else:
                self.conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=? '
                                  f'AND origin_id IN {_STARTER_ORIGINS}', (aid,))
                provenance.invalidate_explanation(self.conn,aid)

    @snapshot_read
    def starter_track_rows(self, maximum_duration):
        """Only retained, available songs from the currently selected provider identities."""
        rows = self.conn.execute(
            'SELECT DISTINCT c.identity_key AS key,c.video_id,c.title,c.artist,c.album,c.thumbnail,'
            'c.duration_s AS duration,p.provider_id AS seed,c.recording_id FROM discovery_sources s '
            'JOIN discovery_origins o ON o.id=s.origin_id '
            'JOIN catalog_tracks c ON c.recording_id=s.recording_id AND c.video_id=s.input_video_id '
            "JOIN artist_provider_ids p ON p.provider='youtube' AND p.artist_id=o.artist_id "
            'JOIN discovery_artist_sources a ON a.artist_id=p.artist_id '
            'JOIN discovery_origins starter ON starter.id=a.origin_id '
            "WHERE o.kind='artist' AND starter.kind='artist_start' AND starter.artist_id=o.artist_id "
            "AND c.video_id IS NOT NULL AND c.title<>'' AND COALESCE(c.available,1)<>0 "
            'AND (c.duration_s IS NULL OR c.duration_s<=?) '
            'ORDER BY p.artist_id,s.first_found_at,c.recording_id', (maximum_duration,))
        return [dict(row) for row in rows]

    @snapshot_read
    def interested_artists(self, *, weights, limit=None) -> list:
        """Engaged artists, highest interest first. `limit` (#52) keeps only the top-N so discovery
        scans a focused set instead of every artist you have ever touched."""
        rows = self.conn.execute(
            "WITH days AS MATERIALIZED (SELECT identity_key,COUNT(*) n FROM listening_daily_items "
            "                          GROUP BY identity_key), "
            "     credits AS MATERIALIZED (SELECT DISTINCT r.identity_key,ta.artist_id "
            f"         FROM ({LIBRARY_RECORDINGS_SQL}) r JOIN track_sources e ON e.recording_id=r.recording_id "
            f"         JOIN {self.entry_artists_table} ta ON ta.entry_id=e.id), "
            "     plays AS (SELECT c.artist_id a,SUM(d.n) n FROM days d "
            "               JOIN credits c ON c.identity_key=d.identity_key GROUP BY c.artist_id), "
            "     pls AS (SELECT ta.artist_id a, COUNT(DISTINCT pt.playlist_id) n "
            "               FROM playlist_tracks pt JOIN catalog_tracks t ON t.id=pt.track_id "
            f"               JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id "
            "               JOIN active_playlists p ON p.id=pt.playlist_id "
            f"              WHERE p.playlist_scope_id NOT IN {_GENERATED_PL} "
            "               GROUP BY ta.artist_id), "
            "     sv AS (SELECT aa.artist_id a,COUNT(DISTINCT m.album_id) n FROM saved_album_memberships m "
            "            JOIN album_artists aa ON aa.album_id=m.album_id GROUP BY aa.artist_id), "
            "     seeds AS (SELECT s.artist_id a,MAX(s.score) n FROM discovery_artist_sources s "
            f"       WHERE s.origin_id IN {_STARTER_ORIGINS} GROUP BY s.artist_id), "
            "     arts AS (SELECT a FROM plays UNION SELECT a FROM pls UNION SELECT a FROM sv UNION SELECT a FROM seeds) "
            "SELECT a.name artist,a.id artist_id,ap.provider_id browse_id, "
            "       COALESCE(plays.n,0)*? + COALESCE(pls.n,0)*? + COALESCE(sv.n,0)*? + COALESCE(seeds.n,0) score "
            "FROM arts JOIN artists a ON a.id=arts.a LEFT JOIN artist_provider_ids ap "
            "ON ap.artist_id=a.id AND ap.provider='youtube' "
            "LEFT JOIN plays ON plays.a=arts.a LEFT JOIN pls ON pls.a=arts.a "
            "LEFT JOIN sv ON sv.a=arts.a LEFT JOIN seeds ON seeds.a=arts.a ORDER BY score DESC"
            + (" LIMIT ?" if limit is not None else ""),
            (*weights, limit) if limit is not None else tuple(weights)).fetchall()
        return [dict(r) for r in rows]

    @snapshot_read
    def artist_scan_times(self, artist_ids):
        return dict(self.conn.execute(
            'SELECT id,discography_scanned_at FROM artists WHERE discography_scanned_at IS NOT NULL '
            'AND id IN (SELECT value FROM json_each(?))', (json.dumps(artist_ids),)))

    @snapshot_read
    def discovered_albums_for_artist(self, aid) -> dict:
        """{browse_id: offered_count} for one artist's pooled albums (#52 rotation input)."""
        return {r["browse_id"]: (r["offered_count"] or 0) for r in self.conn.execute(
            'SELECT browse_id,offered_count FROM discovered_albums WHERE artist_id=?', (aid,))}

    @synchronized
    def delete_discovered_albums(self, browse_ids) -> int:
        """Delete the given pooled albums (rotation / bounds enforcement). Returns the count removed."""
        ids = list({album_aliases.primary_id(self.conn, bid) for bid in browse_ids})
        if not ids:
            return 0
        self.conn.executemany("DELETE FROM discovered_albums WHERE browse_id=?", [(b,) for b in ids])
        self.conn.commit()
        return len(ids)

    @snapshot_read
    def orphan_track_keys(self):
        """Drop discovered tracks whose source album is no longer in the pool (#52 cleanup). Radio-
        and genre-sourced tracks have their own lifecycle and are kept. Returns the
        count removed."""
        rows = self.conn.execute(
            "SELECT r.identity_key FROM recordings r WHERE r.discovery_entry_id IS NOT NULL "
            "AND EXISTS(SELECT 1 FROM discovery_sources s WHERE s.recording_id=r.id) "
            'AND NOT EXISTS(SELECT 1 FROM discovery_sources s JOIN discovery_origins o ON o.id=s.origin_id '
            "WHERE s.recording_id=r.id AND (o.kind<>'album' "
            'OR o.album_id IN (SELECT id FROM albums WHERE discovery_found_at IS NOT NULL)))')
        return {row['identity_key'] for row in rows}

    @synchronized
    def mark_scanned(self, aid, now) -> None:
        with self.conn:
            changed = self.conn.execute('UPDATE artists SET discography_scanned_at=? WHERE id=?', (now, aid)).rowcount
            if changed != 1:
                raise ValueError('Unknown artist for discography scan')

    # --- the accumulating pools ---
    @synchronized
    def add_album_membership(self, album_id, now):
        with self.conn:
            self.conn.execute('UPDATE albums SET discovery_found_at=? WHERE id=? AND discovery_found_at IS NULL',
                              (now, album_id))

    @snapshot_read
    def origin_inputs(self, value, account):
        video = self.conn.execute('SELECT video_id FROM track_sources WHERE video_id=?', (value,)).fetchone()
        album = self.conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?", (value,)).fetchone()
        return dict(video=video[0] if video else None, album=album[0] if album else None,
            account=self.conn.execute('SELECT 1 FROM accounts WHERE id=?', (account,)).fetchone() is not None,
            artists={artist_keys.canonical_id(self.conn, row[0]) for row in self.conn.execute(
                "SELECT artist_id FROM artist_provider_ids WHERE provider='youtube' AND provider_id=?", (value,))})

    @synchronized
    def store_discovered_artist(self, aid, score, thumbnail, now, genre, *, reference, file_id, origin, genre_key):
        from yt_playlist.repos import collection_file_writes
        with self.conn.atomic():
            self._store_artist_source(aid, score, origin)
            candidates.admit_artist(self.conn, aid, score, thumbnail, now, refresh_score=True)
            collection_file_writes.complete(self.conn, file_id, 'discovery-candidate', aid, reference)
            provenance.write_explanation_reference(self.conn, aid, reference)
            provenance.refresh_counts(self.conn,'artist',aid)
            if genre is not None:
                self.conn.execute('UPDATE artists SET discovery_genre_key=?,discovery_unresolved_genre=? WHERE id=?',
                                  (genre_key, genre if genre_key is None else None, aid))

    @lookup_read
    def artist_images_due(self, artist_ids, now):
        """Only the bounded on-screen cohort, with a day between failed lookups."""
        ids = list(artist_ids)
        if not ids:
            return []
        qs = ','.join('?' for _ in ids)
        return [dict(r) for r in self.conn.execute(
            'SELECT d.artist_id,a.name artist FROM discovered_artists d '
            'JOIN artists a ON a.id=d.artist_id '
            f"WHERE d.artist_id IN ({qs}) AND COALESCE(d.thumbnail,'')='' "
            'AND COALESCE(a.discovery_image_retry_at,0)<=? ORDER BY d.artist_id', (*ids, now))]

    @synchronized
    def record_artist_image(self, artist_id, thumbnail, *, retry_at):
        """An image lookup cannot overwrite rankings, provenance or exposure counts."""
        with self.conn:
            self.conn.execute('UPDATE artists SET discovery_image_retry_at=? WHERE id=?', (retry_at, artist_id))
            if thumbnail:
                self.conn.execute("UPDATE artists SET discovery_thumbnail=? WHERE id=? AND discovery_candidate=1 "
                                  "AND COALESCE(discovery_thumbnail,'')=''", (thumbnail, artist_id))

    @synchronized
    def retain_discovered_artist(self, aid, score, *, origin, excluded_kinds):
        """Retain independent interest without overwriting an existing suggestion's metadata."""
        if self.conn.execute(
                'SELECT 1 FROM discovered_artists WHERE artist_id=?', (aid,)).fetchone():
            with self.conn:
                independent = self._other_source_predicate(excluded_kinds)
                self.conn.execute('UPDATE artists SET discovery_score=? WHERE id=? AND discovery_candidate=1 '
                    'AND NOT EXISTS (SELECT 1 FROM discovery_artist_sources WHERE artist_id=? '
                    'AND '+independent+')', (score, aid, aid, *excluded_kinds))
                self._store_artist_source(aid, score, origin)

    def _other_source_predicate(self, excluded_kinds):
        slots = ','.join('?' for _ in excluded_kinds)
        return f'origin_id IN (SELECT id FROM discovery_origins WHERE kind NOT IN ({slots}))'

    def _store_artist_source(self, aid, score, origin):
        provenance.retain_artist(self.conn, aid, provenance.store_origin(self.conn, origin), score)

    @synchronized
    def seed_spotify_artists(self, rows, now, *, origin, excluded_kinds):
        """Retain import provenance even when an artist was independently discovered."""
        seeded = 0
        with self.conn:
            for aid, plays in rows:
                self._store_artist_source(aid, plays, origin)
                seeded += self.conn.execute(
                    'UPDATE artists SET discovery_candidate=1,discovery_score=?,discovery_found_at=? '
                    'WHERE id=? AND discovery_candidate=0', (float(plays), now, aid)).rowcount
                independent = self._other_source_predicate(excluded_kinds)
                self.conn.execute('UPDATE artists SET discovery_score=('
                    'SELECT MAX(score) FROM discovery_artist_sources WHERE artist_id=?) WHERE id=? AND discovery_candidate=1 '
                    'AND NOT EXISTS (SELECT 1 FROM discovery_artist_sources WHERE artist_id=? '
                    'AND '+independent+')', (aid, aid, aid, *excluded_kinds))
                provenance.refresh_counts(self.conn,'artist',aid)
        return seeded

    @snapshot_read
    def get_discovered_albums(self) -> list:
        return [{"browse_id": r["browse_id"], "artist": r["artist"], "artist_id":r['artist_id'], "title": r["title"],
                 "year": r["year"], "thumbnail": r["thumbnail"], "found_at": r["found_at"],
                 "last_shown": r["last_shown"], "genre": r["genre"],
                 "first_shown": r["first_shown"], "offered_count": r["offered_count"]}
                for r in self.conn.execute('SELECT * FROM discovered_albums')]

    @snapshot_read
    def present_home_tiles(self, album_ids, artist_ids, *, recheck_artist_ownership=False):
        """Check a bounded prepared selection without reading/scoring either full pool."""
        albums, artists_found = {}, {}
        if album_ids:
            aliases = {bid: album_aliases.primary_id(self.conn, bid) for bid in album_ids}
            qs = ",".join("?" for _ in album_ids)
            albums = {r["browse_id"]: dict(r) for r in self.conn.execute(
                'SELECT browse_id,artist,artist_id,title,genre,year,thumbnail FROM discovered_albums '
                f'WHERE browse_id IN ({qs})', list(aliases.values()))}
            albums = {bid: albums[preferred]
                      for bid, preferred in aliases.items() if preferred in albums}
        if artist_ids:
            qs = ",".join("?" for _ in artist_ids)
            ownership = ''
            if recheck_artist_ownership:
                credited = library_recordings_sql(
                    f'SELECT DISTINCT r.id,r.identity_key FROM {self.entry_artists_table} credit '
                    'JOIN track_sources source ON source.id=credit.entry_id '
                    'JOIN recordings r ON r.id=source.recording_id WHERE credit.artist_id=d.artist_id')
                ownership = (
                    ' AND (EXISTS (SELECT 1 FROM discovery_artist_sources s WHERE s.artist_id=d.artist_id '
                    f'AND s.origin_id IN {_STARTER_ORIGINS}) OR NOT EXISTS (SELECT 1 FROM ({credited})))')
            artists_found = {r["artist_id"]: dict(r) for r in self.conn.execute(
                f'SELECT d.artist_id,a.name artist,{self._artist_genre} AS genre,d.thumbnail FROM discovered_artists d '
                f"JOIN artists a ON a.id=d.artist_id WHERE d.artist_id IN ({qs})" + ownership, list(artist_ids))}
        return albums, artists_found

    @snapshot_read
    def discovered_artist_rows(self) -> list:
        return [dict(row) for row in self.conn.execute(
            f'SELECT {self._artist_genre} AS genre,d.*,a.name display_artist FROM discovered_artists d '
            'JOIN artists a ON a.id=d.artist_id')]

    @property
    def _artist_genre(self):
        return genre_classifications.read_expression(self.conn, 'd', 'discovered_artists')

    @synchronized
    def add_track_membership(self, rid, entry_id, now):
        with self.conn:
            candidates.admit_track(self.conn, rid, entry_id, now)
            provenance.refresh_counts(self.conn,'track',rid)

    @lookup_read
    def pooled_album(self, browse):
        return self.conn.execute('SELECT 1 FROM discovered_albums WHERE browse_id=?',
            (album_aliases.primary_id(self.conn, browse),)).fetchone() is not None

    @synchronized
    def retain_track_source(self, rid, video, now, origin):
        with self.conn:
            provenance.retain(self.conn, rid, provenance.store_origin(self.conn, origin), video, now)

    @snapshot_read
    def discovery_sources_for_track(self, key) -> list:
        return [dict(row) for row in self.conn.execute(
            "SELECT s.*,s.first_found_at found_at,t.source_token,o.kind source_type,"
            "CASE o.kind WHEN 'radio' THEN o.seed_video_id WHEN 'genre' THEN o.genre_key "
            "WHEN 'album' THEN substr(t.source_token,1) WHEN 'artist' THEN substr(t.source_token,8) "
            "ELSE t.source_token END source_id,o.resolution_status,o.resolution_reason "
            'FROM main.discovery_sources s JOIN recordings r ON r.id=s.recording_id '
            'JOIN main.discovery_origins o ON o.id=s.origin_id JOIN main.discovery_origin_tokens t ON t.id=o.id '
            "WHERE r.identity_key=? ORDER BY s.first_found_at,t.source_token,s.id", (key,))]

    @snapshot_read
    def discovered_source_tokens(self, source_type=None) -> set:
        return {row[0] for row in self.conn.execute(
            'SELECT DISTINCT t.source_token FROM main.discovery_sources s JOIN main.discovery_origin_tokens t ON t.id=s.origin_id'
            + (" WHERE t.kind=?" if source_type else ""),
            (source_type,) if source_type else ())}

    @contextmanager
    def cleanup_snapshot(self):
        if self._lock.held_by_current_thread:
            raise RuntimeError('Discovery cleanup must prepare outside the serving lock')
        # Capture table counters, without copying every historical per-record
        # revision. Publication still guards only the tables actually consumed.
        with ExitStack() as stack:
            with self._lock:
                dependencies = {(table, '') for table in self._store._snapshot_revisions.tables}
                dependencies.add(('recommendation_generation_surfaces', 'group:selection'))
                snapshot = stack.enter_context(self._store.read_snapshot(dependencies=dependencies))
            yield snapshot

    @snapshot_read
    def cleanup_head_rows(self, *, limit):
        with column_group_reads(self._store, 'recommendation_generation_surfaces', 'selection'):
            return [dict(row) for row in self.conn.execute(
                'SELECT surface,generation_id FROM recommendation_generation_surfaces '
                'WHERE selected=1 ORDER BY surface LIMIT ?', (limit,))]

    @staticmethod
    def _dtrack_row(r) -> dict:
        d = {k: r[k] for k in _DTRACK_FIELDS}
        d["audio"] = {c: r[c] for c in _DTRACK_AUDIO if r[c] is not None}
        return d

    @synchronized
    def record_discovery_impressions(self, kind, ids, view_id, now) -> None:
        """Count each visible Home Discover tile once per page visit, including across poll retries."""
        with self.conn.atomic(join=True):
            for value in dict.fromkeys(ids):
                identifier = provenance.subject(self.conn,kind,value)
                if identifier is not None:
                    provenance.impression(self.conn,kind,identifier,view_id,now)

    @snapshot_read
    def get_discovered_tracks(self) -> list:
        """Complete ranking inputs, detached and reusable against this reader's inputs."""
        def prepare():
            cursor = self.conn.execute(f'SELECT {_DTRACK_PROJECTION} FROM discovered_tracks')
            rows = []
            while page := cursor.fetchmany(512):
                rows.extend(self._dtrack_row(row) for row in page)
            return rows
        return self._store.memoized_read('discovered-tracks', prepare)

    @snapshot_read
    def discovered_track_keys(self) -> set[str]:
        """Canonical pool membership without catalog metadata, audio or provenance."""
        return {row[0] for row in self.conn.execute(
            'SELECT identity_key FROM recordings WHERE discovery_entry_id IS NOT NULL').fetchall()}

    @snapshot_read
    def identity_keys_for_video(self, video_id) -> list[str]:
        """Resolve discovery membership without decoding unrelated metadata or cached-only items."""
        return [r[0] for r in self.conn.execute(
            'SELECT r.identity_key FROM track_sources e JOIN recordings r ON r.discovery_entry_id=e.id '
            'WHERE e.video_id=?', (video_id,))]

    @snapshot_read
    def discovered_tracks_by_keys(self, keys, *, available_only=False, metadata_only=False) -> dict:
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        available = " AND available IS NOT 0" if available_only else ""
        if metadata_only:
            return {r['identity_key']: dict(r) for r in self.conn.execute(
                f'SELECT identity_key,genre,year FROM discovered_tracks WHERE identity_key IN ({qs})' + available, keys)}
        return {r["identity_key"]: self._dtrack_row(r) for r in self.conn.execute(
            f'SELECT {_DTRACK_PROJECTION} FROM discovered_tracks '
            f"WHERE identity_key IN ({qs})" + available, keys)}

    @snapshot_read
    def playable_discoveries_by_keys(self, keys):
        """Keep the preferred Discovery upload when playable, or find an alternative."""
        keys = tuple(dict.fromkeys(keys))
        if not keys:
            return {}
        placeholders = ','.join('?' for _ in keys)
        rows = self.conn.execute(
            f'''SELECT r.identity_key,t.video_id,t.title,t.artist,t.album,t.thumbnail,t.genre
                FROM recordings r
                LEFT JOIN catalog_tracks preferred ON preferred.id=r.discovery_entry_id
                    AND {is_playable_sql('preferred')}
                JOIN catalog_tracks t ON t.id=coalesce(preferred.id,(
                    SELECT upload.id FROM catalog_tracks upload
                    WHERE upload.recording_id=r.id AND {is_playable_sql('upload')}
                    ORDER BY upload.id LIMIT 1))
                WHERE r.identity_key IN ({placeholders}) AND r.discovery_entry_id IS NOT NULL''', keys)
        return {row['identity_key']: dict(row) for row in rows}

    @snapshot_read
    def candidate_tracks_by_keys(self, keys) -> dict:
        """Current playable candidate metadata without audio or offer history.

        Musical evidence has its own prepared profile owner. Selecting every
        discovery field here also evaluates unused metadata and provenance
        subqueries for each recording in a Vibe's overlapping rank pages.
        """
        keys = tuple(dict.fromkeys(keys))
        if not keys:
            return {}
        placeholders = ','.join('?' for _ in keys)
        rows = self.conn.execute(
            'SELECT identity_key,recording_id,video_id,title,artist,album,thumbnail,genre,year '
            f'FROM discovered_tracks WHERE identity_key IN ({placeholders}) '
            'AND available IS NOT 0', keys)
        return {row['identity_key']: dict(row) for row in rows}

    @snapshot_read
    def discovery_track_rows(self) -> list:
        """Discovered tracks with play count (history), library-playlist membership count, offered
        count, saved metadata, and expiry (using found_at until shown). Newest first."""
        rows = self.conn.execute(
            "WITH plays AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key,COUNT(*) n FROM listening_days "
            "                            GROUP BY identity_key), "
            "     memberships AS MATERIALIZED (SELECT t.identity_key,COUNT(DISTINCT pt.playlist_id) n "
            "         FROM catalog_tracks t JOIN playlist_tracks pt ON pt.track_id=t.id GROUP BY t.identity_key) "
            'SELECT dt.*,COALESCE(p.n,0) plays,COALESCE(m.n,0) playlists FROM discovered_tracks dt '
            "LEFT JOIN plays p ON p.identity_key=dt.identity_key "
            "LEFT JOIN memberships m ON m.identity_key=dt.identity_key ORDER BY dt.found_at DESC").fetchall()
        out = []
        for r in rows:
            d = self._dtrack_row(r)
            d.update(plays=r["plays"], playlists=r["playlists"])
            out.append(d)
        return out

    @snapshot_read
    def discovery_album_rows(self) -> list:
        rows = self.conn.execute('SELECT * FROM discovered_albums ORDER BY found_at DESC').fetchall()
        return [{k: r[k] for k in ("browse_id", "artist", "title", "year", "thumbnail", "genre",
                                  "found_at", "first_shown", "offered_count", "legacy_offered_count")} for r in rows]

    @snapshot_read
    def discovery_artist_rows(self) -> list:
        rows = self.conn.execute(
            f'SELECT {self._artist_genre} AS genre,d.*,a.name display_artist,'
            'a.earlier_offer_count AS legacy_offered_count FROM discovered_artists d '
            'JOIN artists a ON a.id=d.artist_id ORDER BY found_at DESC').fetchall()
        return [{"artist":r['display_artist'],'artist_id':r['artist_id'], **{k: r[k] for k in ("score", "thumbnail", "genre",
                                      "found_at", "first_shown", "offered_count", "legacy_offered_count")}} for r in rows]

    @snapshot_read
    def expired_pool_rows(self, cutoff):
        """Query expired memberships, preserving recent sources and starter choices."""
        expired_albums = [row[0] for row in self.conn.execute(
            'SELECT browse_id FROM discovered_albums WHERE first_shown IS NOT NULL AND first_shown < ?',
            (cutoff,))]
        expired_artists = [row[0] for row in self.conn.execute(
            'SELECT d.artist_id FROM discovered_artists d WHERE first_shown IS NOT NULL AND first_shown < ? '
            'AND NOT EXISTS(SELECT 1 FROM main.discovery_artist_sources s WHERE s.artist_id=d.artist_id '
            f'AND s.origin_id IN {_STARTER_ORIGINS})', (cutoff,))]
        retained_starter = (
            'JOIN main.discovery_origins o ON o.id=s.origin_id '
            'JOIN main.discovery_artist_sources a ON a.artist_id=o.artist_id '
            'JOIN main.discovery_origins starter ON starter.id=a.origin_id '
            "WHERE s.recording_id=d.recording_id AND o.kind='artist' "
            "AND starter.kind='artist_start' AND starter.artist_id=o.artist_id")
        tracks = [row[0] for row in self.conn.execute(
            'SELECT d.identity_key FROM discovered_tracks d '
            "WHERE COALESCE(first_shown, found_at) < ? "
            'AND NOT EXISTS(SELECT 1 FROM main.discovery_sources s WHERE '
            "s.recording_id=d.recording_id AND s.last_found_at>=?) "
            'AND NOT EXISTS(SELECT 1 FROM main.discovery_sources s ' + retained_starter + ')', (cutoff, cutoff))]
        return {'albums': expired_albums, 'artists': expired_artists, 'tracks': tracks}

    @snapshot_read
    def cleanup_source_tokens(self, keys):
        keys = list(keys)
        sources = set()
        for offset in range(0, len(keys), 500):
            page = keys[offset:offset + 500]
            marks = ','.join('?' for _ in page)
            sources.update(row[0] for row in self.conn.execute(
                'SELECT t.source_token FROM discovery_sources s '
                'JOIN discovery_origin_tokens t ON t.id=s.origin_id ' +
                'JOIN recordings r ON r.id=s.recording_id WHERE r.identity_key IN (' + marks + ') AND ' +
                "t.kind IN ('album','radio','genre','artist')", page))
        return sources

    @synchronized
    def remember_source_tokens(self, tokens):
        from yt_playlist.core import discovery_checkpoints
        with self.conn.atomic():
            discovery_checkpoints.remember(self.conn, tokens)

    @synchronized
    def delete_pool_entries(self, albums, artists, tracks):
        with self.conn.atomic():
            self.conn.executemany('DELETE FROM main.discovered_albums WHERE browse_id=?', [(bid,) for bid in albums])
            self.conn.executemany(f'UPDATE artists SET {candidates.CLEAR_ARTIST} '
                                  'WHERE id=? AND discovery_candidate=1', [(aid,) for aid in artists])
            self.conn.executemany(f'UPDATE recordings SET {candidates.CLEAR_TRACK} '
                                  'WHERE identity_key=? AND discovery_entry_id IS NOT NULL', [(key,) for key in tracks])

    @snapshot_read
    def prunable_album_rows(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT d.browse_id,p.album_id FROM discovered_albums d LEFT JOIN album_provider_ids p "
            "ON p.provider='youtube' AND p.provider_id=d.browse_id")]

    @snapshot_read
    def prunable_artist_ids(self):
        return [row[0] for row in self.conn.execute(
            "SELECT artist_id FROM discovered_artists d WHERE NOT EXISTS("
            'SELECT 1 FROM discovery_artist_sources s WHERE s.artist_id=d.artist_id '
            f'AND s.origin_id IN {_STARTER_ORIGINS})')]
