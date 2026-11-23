"""TrackRepo: track rows and their genre/year enrichment."""
from contextlib import contextmanager
from yt_playlist.core import association_work, catalog, metadata, rating_actions, recording_library, track_sources
from yt_playlist.core.album_queries import TRACK_LINKS
from yt_playlist.core.catalog_keys import resolve_recording
from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read
from yt_playlist.repos.album_navigation import track_album_browse_ids


class TrackRepo(Repo):
    @synchronized
    def prefer_playback_upload(self, original, actual):
        with self.conn.atomic(join=True):
            recording = self.conn.execute(
                'SELECT r.id,r.identity_key FROM track_sources q JOIN recordings r ON r.id=q.recording_id '
                'JOIN track_sources p ON p.recording_id=r.id WHERE q.video_id=? AND p.video_id=?',
                (original, actual)).fetchone()
            if recording is not None:
                track_sources.prefer_playback(self.conn, recording['id'], actual)
                return recording['identity_key']

    @lookup_read
    def preferred_playback_uploads(self, video_ids):
        videos = tuple(dict.fromkeys(video_ids))
        if not videos:
            return {}
        result = {}
        slots = ','.join('?' for _ in videos)
        rows = self.conn.execute(
            'SELECT q.video_id AS requested_video_id,c.*,c.identity_key AS key,c.duration_s AS duration '
            'FROM track_sources q JOIN track_sources p ON p.recording_id=q.recording_id '
            'JOIN catalog_tracks c ON c.id=p.id '
            f'WHERE q.video_id IN ({slots}) AND p.id<>q.id AND p.video_id IS NOT NULL '
            'AND p.effective_available IS NOT 0 '
            'AND (q.effective_available=0 OR (p.playback_preferred=1 AND q.playback_preferred=0)) '
            'ORDER BY p.playback_preferred DESC,p.library_origin IS NULL,p.id', videos)
        for row in rows:
            result.setdefault(row['requested_video_id'], dict(row))
        return result

    @snapshot_read
    def track_detail(self, video_id):
        """Read the exact upload, with the catalog's effective metadata choices."""
        row = self.conn.execute('SELECT * FROM catalog_tracks WHERE video_id=?',
                                (video_id,)).fetchone()
        if row is None:
            return None
        track = dict(row)
        track['album_browse'] = track.get('album_browse_id')
        if not track['album_browse'] and track.get('album'):
            track['album_browse'] = track_album_browse_ids(
                self.conn, self.album_tracks_table, [track['id']]).get(track['id'])
        track['duration'] = track.get('duration_s')
        track['year'] = track.get('mb_year')
        track['playlists'] = [dict(p) for p in self.conn.execute(
            'SELECT p.id,p.title,i.label identity FROM playlist_items pi '
            'JOIN active_playlists p ON p.id=pi.playlist_id JOIN accounts i ON i.id=p.account_id '
            'WHERE pi.track_source_id=(SELECT id FROM track_sources WHERE video_id=?) '
            'ORDER BY p.title COLLATE NOCASE,p.id', (video_id,))]
        upload = self.preferred_playback_uploads([video_id]).get(video_id)
        if upload:
            track['playback_upload'] = {field: upload[field] for field in ('video_id', 'title', 'artist')}
        return track

    @snapshot_read
    def track_play_ledger(self, recording_id, *, page=0, limit=50):
        """Page through a recording's timestamped plays and history observations.

        Confirmed uploads share history. Group repeated observations by source date,
        account, source, precision and timezone. Hide YouTube Music calendar-day rows
        covered by an exact play in the account's listening calendar.
        """
        rows = self.conn.execute(
            "SELECT 'play' kind,pe.id evidence_id,pe.played_at sort_at,pe.played_at,"
            "NULL day,NULL timezone,'exact_timestamp' precision,i.label identity,"
            "(SELECT group_concat(source, ', ') FROM play_event_sources s "
            "WHERE s.event_id=pe.id) source,pe.video_id,pe.playlist_ytm_id,"
            "pe.is_generated FROM play_event_subjects pe JOIN accounts i ON i.id=pe.account_id "
            "WHERE pe.recording_id=? UNION ALL "
            "SELECT 'observation',MIN(o.id),MAX(o.last_observed_at),NULL,o.source_day,o.timezone,"
            "o.precision,i.label,o.source,NULL,NULL,0 "
            "FROM listening_observation_subjects o JOIN accounts i ON i.id=o.account_id "
            "WHERE o.recording_id=? "
            "AND NOT (o.source='ytm_history' AND o.precision='calendar_day' AND EXISTS ("
            "SELECT 1 FROM play_event_subjects pe JOIN play_event_calendar c ON c.event_id=pe.id "
            "WHERE pe.recording_id=o.recording_id AND pe.account_id=o.account_id AND c.day=o.day)) "
            "GROUP BY o.account_id,o.source_day,o.precision,o.source,o.timezone "
            "ORDER BY sort_at DESC,kind,evidence_id DESC LIMIT ? OFFSET ?",
            (recording_id, recording_id, limit + 1, page * limit)).fetchall()
        return [dict(row) for row in rows[:limit]], len(rows) > limit

    @snapshot_read
    def association_subjects(self, video_id, recording_id):
        recording_id = resolve_recording(self.conn, recording_id)
        previous = self.conn.execute('SELECT recording_id FROM track_sources WHERE video_id=?', (video_id,)).fetchone()
        target = self.conn.execute('SELECT id FROM recordings WHERE id=?', (recording_id,)).fetchone()
        if previous is None or target is None:
            raise ValueError('Unknown item or recording')
        keys = {row['id']: row['identity_key'] for row in self.conn.execute(
            'SELECT id,identity_key FROM recordings WHERE id IN (?,?)', (previous[0], target[0]))}
        remaining = self.conn.execute('SELECT MIN(id) FROM track_sources WHERE recording_id=? AND video_id IS NOT ?',
            (previous[0], video_id)).fetchone()[0]
        aliases = [row[0] for row in self.conn.execute(
            'SELECT alias FROM recording_aliases WHERE recording_id=?', (previous[0],))]
        return dict(previous=previous[0], target=target[0], previous_key=keys[previous[0]],
            target_key=keys[target[0]], remaining=remaining, aliases=aliases)

    @synchronized
    def association_work(self, video):
        return association_work.pending(self.conn, video)

    @synchronized
    def save_association_work(self, video, association, plan_json):
        with self.conn.atomic():
            association_work.save(self.conn, video, association, plan_json)

    @synchronized
    def finish_association_work(self, association):
        with self.conn.atomic():
            association_work.finish(self.conn, association)

    @synchronized
    def record_association(self, video, previous, target, source, evidence_json, now):
        with self.conn.atomic(join=True):
            return association_work.append(self.conn, video, target, previous, source, evidence_json, now)

    @synchronized
    def assign_recording(self, video, previous, target, *, association=None):
        from yt_playlist.core import discovery_sources
        with self.conn.atomic(join=True):
            self.conn.execute('DELETE FROM recording_embedding_files WHERE recording_id IN (?,?)', (previous, target))
            self.conn.execute('DELETE FROM prepared_recording_inputs WHERE recording_id IN (?,?)', (previous, target))
            discovery_sources.prepare_reassociation(self.conn, video, previous)
            track_sources.reassign(self.conn, video, target)
            self.conn.execute('UPDATE radio_queue_items SET recording_id=? WHERE video_id=?', (target, video))
            self.conn.execute('UPDATE main.play_events SET recording_id=?,unresolved_subject_key=NULL WHERE video_id=?',
                              (target, video))
            self.conn.execute('UPDATE recordings SET provisional=0 WHERE id=?', (target,))
            association_work.complete(self.conn, association, 'assigned')

    @synchronized
    def redirect_recording(self, previous, target, source, evidence_json, *, association=None):
        with self.conn.atomic(join=True):
            catalog.redirect(self.conn, previous, target, source, evidence_json)
            aliases = [dict(row) for row in self.conn.execute('SELECT alias,source FROM recording_aliases WHERE recording_id=?', (previous,))]
            self.conn.executemany('INSERT OR IGNORE INTO recording_aliases VALUES (?,?,?)',
                [(row['alias'], target, row['source']) for row in aliases])
            self.conn.execute('DELETE FROM recording_aliases WHERE recording_id=?', (previous,))
            association_work.complete(self.conn, association, 'redirected')
        return [row['alias'] for row in aliases]

    @synchronized
    def resolve_identity_key(self, key, video_id=None):
        return catalog.resolve_key(self.conn, key, video_id)

    @snapshot_read
    def recording_match_rows(self, alias):
        return [dict(row) for row in self.conn.execute(
            'SELECT DISTINCT c.recording_id,c.identity_key,c.title,c.artist FROM catalog_tracks c '
            'JOIN recording_aliases a ON a.recording_id=c.recording_id WHERE a.alias=?',
            (alias,))]

    @synchronized
    def catalog_subject(self, track_id):
        return self.track_for_waterfall(track_id)

    @contextmanager
    def catalog_transaction(self):
        with self._lock, self.conn.atomic(join=True):
            self.conn.execute('UPDATE recordings SET identity_key=identity_key WHERE 0')
            yield

    @synchronized
    def entry_for_source(self, video_id, source_ref):
        column = 'video_id' if video_id else 'source_ref'
        row = self.conn.execute('SELECT id,recording_id,video_id,source_ref FROM track_sources WHERE ' + column + '=?',
                                (video_id or source_ref,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def alias_exists(self, alias):
        return self.conn.execute('SELECT 1 FROM recording_aliases WHERE alias=? LIMIT 1', (alias,)).fetchone() is not None

    @lookup_read
    def alias_binding_exists(self, alias, recording):
        return self.conn.execute('SELECT 1 FROM recording_aliases WHERE alias=? AND recording_id=?',
            (alias, recording)).fetchone() is not None

    @synchronized
    def create_recording(self, temporary_key, title, artist, now):
        return self.conn.execute('INSERT INTO recordings(identity_key,title,artist,created_at) VALUES (?,?,?,?)',
            (temporary_key, title, artist, now)).lastrowid

    @synchronized
    def create_entry(self, recording, key, video_id, source_ref, title, artist):
        self.conn.execute('UPDATE recordings SET identity_key=? WHERE id=?', (key, recording))
        return track_sources.create(self.conn, recording, video_id, source_ref, title, artist)

    @synchronized
    def reconcile_admission(self, recording, video_id, now):
        with self.conn.atomic(join=True):
            key = self.conn.execute('SELECT identity_key FROM recordings WHERE id=?', (recording,)).fetchone()[0]
            if video_id:
                association_work.admit(self.conn, video_id, recording, now)
                self.conn.execute('UPDATE main.play_events SET recording_id=?,unresolved_subject_key=NULL '
                    'WHERE video_id=? AND recording_id IS NULL', (recording, video_id))
            self.conn.execute("UPDATE main.play_events SET unresolved_subject_key='@unresolved-video:'||video_id "
                "WHERE recording_id IS NULL AND unresolved_subject_key=? AND NULLIF(trim(video_id),'') IS NOT NULL "
                'AND NOT EXISTS(SELECT 1 FROM track_sources y WHERE y.video_id=play_events.video_id)', (key,))

    @lookup_read
    def catalog_metadata_inputs(self, entry, fields):
        from yt_playlist.core import metadata_runtime
        return {'evidence': not metadata_runtime.active(self.conn),
                'sources': metadata.selected_sources(self.conn, entry, fields)}

    @synchronized
    def add_library_membership(self, entry, now):
        with self.conn.atomic(join=True):
            track_sources.save_to_library(self.conn, (entry,), now)

    @synchronized
    def remove_song_library_membership(self, video):
        """Release a standalone save, preserving playlist and saved-album ownership."""
        with self.conn.atomic(join=True):
            if self.song_library_membership(video)['retained']:
                return False
            track_sources.remove_from_library(self.conn, video)
            self.conn.execute('UPDATE track_sources SET starter_selected_at=NULL '
                              'WHERE video_id=? AND starter_selected_at IS NOT NULL', (video,))
            return True

    @lookup_read
    def song_library_membership(self, video):
        """Exact-upload ownership and owners a standalone removal cannot release."""
        row = self.conn.execute(
            'SELECT e.library_origin IS NOT NULL owned, '
            'EXISTS(SELECT 1 FROM playlist_items p WHERE p.track_source_id=e.id) OR '
            f'EXISTS(SELECT 1 FROM {self.album_tracks_table} a '
            'JOIN saved_album_memberships m ON m.album_id=a.album_id '
            'WHERE a.entry_id=e.id) retained FROM track_sources e WHERE e.video_id=?', (video,)).fetchone()
        retained = bool(row and row['retained'])
        return {'owned': bool(row and row['owned']) or retained, 'retained': retained}

    @lookup_read
    def entry_key(self, entry):
        return self.conn.execute('SELECT identity_key FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0]

    @contextmanager
    def rating_transaction(self):
        """Keep the rating read and its prepared writes in this repository's unit of work."""
        with self._lock:
            with self.conn.atomic(join=True):
                yield

    @snapshot_read
    def item_rating_inputs(self, account_id, video_ids):
        previous, unbanned = {}, {}
        video_ids = list(dict.fromkeys(video_ids))
        for offset in range(0, len(video_ids), 500):
            batch = video_ids[offset:offset + 500]
            slots = ','.join('?' for _ in batch)
            previous.update((row['video_id'], dict(row)) for row in self.conn.execute(
                f'SELECT video_id,status,source,observed_at FROM item_ratings '
                f'WHERE account_id=? AND video_id IN ({slots})', (account_id, *batch)))
            for row in self.conn.execute(
                    'SELECT y.video_id,f.created_at FROM rec_feedback f JOIN track_sources y '
                    'ON y.recording_id=f.recording_id '
                    f"WHERE y.video_id IN ({slots}) AND f.surface='rating_override' AND f.kind='unban'", batch):
                unbanned.setdefault(row['video_id'], row['created_at'])
        return previous, unbanned

    @synchronized
    def write_item_ratings(self, observations, action_rows, *, playlist=None, capture_id=None):
        with self.conn.atomic(join=True):
            rating_actions.append(self.conn, action_rows, playlist=playlist, capture_id=capture_id)
            self.conn.executemany(
                'INSERT INTO item_ratings VALUES (?,?,?,?,?) '
                'ON CONFLICT(account_id,video_id) DO UPDATE SET '
                'source=excluded.source,observed_at=excluded.observed_at,status=excluded.status',
                observations)

    @synchronized
    def retained_rating_statuses(self, account_id, video_ids, *, newer_than=None):
        """Read winning exact-upload ratings, including actions absent from an older snapshot."""
        video_ids = list(video_ids)
        retained = {}
        for offset in range(0, len(video_ids), 500):
            batch = video_ids[offset:offset + 500]
            retained.update(self.conn.execute('SELECT video_id,status FROM item_ratings '
                'WHERE account_id=? AND video_id IN (' + ','.join('?' for _ in batch) + ')',
                (account_id,*batch)))
        if newer_than is not None:
            retained.update(self.conn.execute('SELECT video_id,status FROM item_ratings '
                'WHERE account_id=? AND observed_at>=?', (account_id,newer_than)))
        return retained

    @lookup_read
    def item_rating(self, account_id, video_id):
        row = self.conn.execute(
            "SELECT status,source,observed_at FROM item_ratings WHERE account_id=? AND video_id=?",
            (account_id, video_id)).fetchone()
        return dict(row) if row else None

    @snapshot_read
    def rating_rows(self, keys):
        from yt_playlist.core.rating_feedback import rating_rows

        return rating_rows(self.conn, keys)

    @snapshot_read
    def action_rating_rows(self, account_id):
        return [dict(row) for row in self.conn.execute(
            "SELECT ir.video_id,ir.status,ir.observed_at,r.identity_key FROM item_ratings ir "
            "LEFT JOIN track_sources y ON y.video_id=ir.video_id "
            "LEFT JOIN recordings r ON r.id=y.recording_id "
            "WHERE ir.account_id=? AND ir.source IN ('api','action') "
            "ORDER BY ir.observed_at,ir.video_id", (account_id,))]

    @snapshot_read
    def rating_feedback_rows(self, keys):
        from yt_playlist.core.rating_feedback import feedback_rows

        return feedback_rows(self.conn, keys)

    @lookup_read
    def has_upload_rating(self, video_id):
        from yt_playlist.core.rating_feedback import has_upload_rating

        return has_upload_rating(self.conn, video_id)

    @lookup_read
    def identity_key_for_video(self, video_id) -> str | None:
        """Resolve an observed upload in the catalog, independently of library membership."""
        row = self.conn.execute(
            "SELECT identity_key FROM catalog_tracks WHERE video_id=? LIMIT 1", (video_id,)).fetchone()
        return row["identity_key"] if row else None

    @lookup_read
    def library_identity_key_for_video(self, video_id) -> str | None:
        """Resolve a library upload without treating cached observations as membership."""
        library = recording_library.TRACKS_SQL
        row = self.conn.execute(
            f'SELECT identity_key FROM ({library}) WHERE video_id=? LIMIT 1', (video_id,)).fetchone()
        return row['identity_key'] if row else None

    def identity_keys_for_videos(self, video_ids) -> dict:
        """Resolve a bounded set of uploads without reopening the catalog per video."""
        return {video: key for video, (key, _available) in self.video_identity_and_availability(video_ids).items()}

    @snapshot_read
    def video_identity_and_availability(self, video_ids):
        requested = list(dict.fromkeys(video_ids))
        result = {}
        for offset in range(0, len(requested), 500):
            page = requested[offset:offset + 500]
            placeholders = ','.join('?' for _ in page)
            for row in self.conn.execute(
                    f'SELECT video_id,identity_key,available FROM catalog_tracks WHERE video_id IN ({placeholders})',
                    page):
                result.setdefault(row['video_id'], (row['identity_key'], row['available']))
        return result

    @lookup_read
    def thumbnail_for_video(self, video_id) -> str | None:
        """Cached artwork for this exact upload, including tracks outside the library."""
        row = self.conn.execute(
            "SELECT thumbnail FROM catalog_tracks WHERE video_id=? "
            "AND thumbnail IS NOT NULL AND thumbnail != '' ORDER BY id DESC LIMIT 1",
            (video_id,)).fetchone()
        return row["thumbnail"] if row else None

    @lookup_read
    def duration_for_video(self, video_id):
        """Stored length of this exact upload, including tracks outside the library."""
        row = self.conn.execute(
            "SELECT duration_s FROM catalog_tracks WHERE video_id=? AND duration_s>0 "
            "ORDER BY id DESC LIMIT 1", (video_id,)).fetchone()
        return row["duration_s"] if row else None

    @snapshot_read
    def duration_match_rows(self, alias):
        return [dict(row) for row in self.conn.execute(
            "SELECT DISTINCT recording_id,duration_s,title,artist FROM catalog_tracks "
            "WHERE identity_key=?", (catalog.resolve_key(self.conn, alias),))]

    # A track still has work when anything is missing, INCLUDING its cover: YouTube serves a generic
    # grey disc rather than nothing when it has no art, so a track can be complete on genre/year/audio
    # and still have no real cover. Without these clauses the waterfall would never revisit it and the
    # cover could never arrive. `_` is a LIKE wildcard, hence the ESCAPE.
    _NO_ART = (r"t.thumbnail IS NULL OR t.thumbnail='' "
               r"OR t.thumbnail LIKE '%cover\_track\_default%' ESCAPE '\' "
               r"OR t.thumbnail LIKE '%cover\_album\_default%' ESCAPE '\'")
    _NEEDS = ("t.genre IS NULL OR t.genre='' OR t.mb_year IS NULL OR t.mb_year='' "
              "OR t.bpm IS NULL OR t.danceability IS NULL "
              "OR " + _NO_ART)

    @synchronized
    def tracks_for_waterfall(self, playlist_id) -> list:
        """Playlist tracks the waterfall still has work for (missing genre, year, or any of the
        core audio features) in playlist order. Carries mb_recording_id so AcousticBrainz can key
        off an already-resolved MBID."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist, t.mb_recording_id, t.thumbnail FROM playlist_tracks pt "
            f"JOIN catalog_tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? AND ({self._NEEDS}) "
            "ORDER BY pt.position", (playlist_id,)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def track_for_waterfall(self, track_id) -> dict | None:
        """One track for an explicit user-requested lookup, even if other metadata is complete."""
        row = self.conn.execute(
            "SELECT id, recording_id, identity_key, video_id, title, artist, mb_recording_id, thumbnail, duration_s FROM catalog_tracks WHERE id=?",
            (track_id,)).fetchone()
        return dict(row) if row is not None else None

    @synchronized
    def album_tracks_for_waterfall(self, album_browse_id) -> list:
        """A saved album's folded-in tracks the waterfall still has work for, album-scoped twin of
        tracks_for_waterfall."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist, t.mb_recording_id, t.thumbnail FROM catalog_tracks t "
            f"JOIN {TRACK_LINKS} a ON a.entry_id=t.id "
            f"WHERE a.browse_id=? AND ({self._NEEDS}) ORDER BY t.id", (album_browse_id,)).fetchall()
        return [dict(r) for r in rows]

    @lookup_read
    def artwork_inputs(self, track_id):
        row = self.conn.execute(
            'SELECT id,thumbnail FROM catalog_tracks WHERE id=?', (track_id,)).fetchone()
        return {**row, 'chosen': metadata.has_choice(self.conn, track_id, 'thumbnail')} if row is not None else None

    @synchronized
    def artwork_source(self, track_id):
        """Current artwork origin; an explicit choice always belongs to the user."""
        if metadata.has_choice(self.conn, track_id, 'thumbnail'):
            return 'user'
        return metadata.selected_source(self.conn, track_id, 'thumbnail')

    @synchronized
    def metadata_for_video(self, video_id) -> dict | None:
        row = self.conn.execute(
            "SELECT id track_id, title, artist, COALESCE(genre, '') genre, "
            "COALESCE(mb_year, '') year, orig_title, orig_artist "
            "FROM catalog_tracks WHERE video_id=?", (video_id,)).fetchone()
        return dict(row) if row else None

    @lookup_read
    def original_metadata(self, track_id):
        row = self.conn.execute('SELECT id,orig_title,orig_artist FROM catalog_tracks WHERE id=?', (track_id,)).fetchone()
        return dict(row) if row is not None else None

    @synchronized
    def get_track_enrichment(self, track_id):
        """Current (genre, year) for a track, used to report the effective value after a fill."""
        row = self.conn.execute("SELECT genre, mb_year FROM catalog_tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            return ("", "")
        return (row["genre"] or "", row["mb_year"] or "")

    @synchronized
    def get_track_audio(self, track_id):
        """Current tempo and danceability for a track."""
        row = self.conn.execute(
            "SELECT bpm, danceability FROM catalog_tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            return (None, None)
        return (row["bpm"], row["danceability"])

    @synchronized
    def track_ids_for_videos(self, video_ids) -> dict:
        """Map video_id -> track_id for tracks already in the store (latest row wins)."""
        out = {}
        for vid in video_ids:
            row = self.conn.execute(
                "SELECT id FROM catalog_tracks WHERE video_id=? ORDER BY id DESC LIMIT 1", (vid,)).fetchone()
            if row is not None:
                out[vid] = row["id"]
        return out

    @synchronized
    def materialized_album_ids(self) -> set:
        """Releases with any library track. This does not prove full materialization."""
        return {r["browse_id"] for r in self.conn.execute(
            f"SELECT DISTINCT a.browse_id FROM {TRACK_LINKS} a "
            "JOIN track_sources e ON e.id=a.entry_id AND e.library_origin IS NOT NULL")}
