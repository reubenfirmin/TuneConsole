"""TrackRepo: track rows and their genre/year enrichment."""
from contextlib import contextmanager
from yt_playlist.core import catalog, entity_names, metadata
from yt_playlist.core.catalog_keys import resolve_recording
from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read


class TrackRepo(Repo):
    @synchronized
    def store_playback_alternate(self, account_id, original, actual, *, same_recording,
                                 evidence_json, now):
        with self.conn:
            alternates = entity_names.owner(self.conn, 'youtube_playback_alternates')
            self.conn.execute(
                f'INSERT INTO {alternates.table} VALUES (?,?,?,?,?,?) '
                f'ON CONFLICT({alternates.column("account_id")},requested_video_id) DO UPDATE SET '
                'video_id=excluded.video_id,same_recording=excluded.same_recording,'
                'evidence_json=excluded.evidence_json,observed_at=excluded.observed_at',
                (account_id, original, actual, int(same_recording), evidence_json, now))

    @synchronized
    def playback_alternate(self, account_id, video_id):
        return self.playback_alternates(account_id, [video_id]).get(video_id)

    @synchronized
    def playback_alternates(self, account_id, video_ids):
        result = {}
        video_ids = list(dict.fromkeys(video_ids))
        for offset in range(0, len(video_ids), 500):
            batch = video_ids[offset:offset + 500]
            rows = self.conn.execute(
                'SELECT a.requested_video_id,c.*,c.identity_key AS key,c.duration_s AS duration '
                'FROM youtube_playback_alternates a JOIN catalog_tracks c ON c.video_id=a.video_id '
                'WHERE a.account_id=? AND a.requested_video_id IN (' + ','.join('?' for _ in batch) + ') '
                'AND a.same_recording=1 AND COALESCE(c.available,1)<>0', (account_id, *batch)).fetchall()
            result.update((row['requested_video_id'], dict(row)) for row in rows)
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
        track['duration'] = track.get('duration_s')
        track['year'] = track.get('mb_year')
        track['playlists'] = [dict(p) for p in self.conn.execute(
            'SELECT p.id,p.title,i.label identity FROM playlist_items pi '
            'JOIN playlists p ON p.id=pi.playlist_id JOIN accounts i ON i.id=p.account_id '
            'WHERE pi.video_id=? ORDER BY p.title COLLATE NOCASE,p.id', (video_id,))]
        track['playback_alternates'] = [dict(row) for row in self.conn.execute(
            'SELECT DISTINCT c.video_id,c.title,c.artist FROM youtube_playback_alternates a '
            'JOIN catalog_tracks c ON c.video_id=a.video_id '
            'WHERE a.requested_video_id=? AND a.same_recording=1 ORDER BY c.video_id', (video_id,))]
        return track

    @snapshot_read
    def track_play_ledger(self, recording_id, *, page=0, limit=50):
        """Bounded evidence ledger; an imported observation is never an exact play.

        All confirmed uploads of this recording share history. Playlist membership and
        ratings remain exact-upload facts. Repeated history snapshots collapse by day,
        account, source and precision rather than manufacturing additional plays.
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
            "GROUP BY o.account_id,o.source_day,o.precision,o.source,o.timezone "
            "ORDER BY sort_at DESC,kind,evidence_id DESC LIMIT ? OFFSET ?",
            (recording_id, recording_id, limit + 1, page * limit)).fetchall()
        return [dict(row) for row in rows[:limit]], len(rows) > limit

    @snapshot_read
    def association_subjects(self, video_id, recording_id):
        recording_id = resolve_recording(self.conn, recording_id)
        previous = self.conn.execute('SELECT recording_id FROM youtube_items WHERE video_id=?', (video_id,)).fetchone()
        target = self.conn.execute('SELECT id FROM recordings WHERE id=?', (recording_id,)).fetchone()
        if previous is None or target is None:
            raise ValueError('Unknown item or recording')
        keys = {row['id']: row['identity_key'] for row in self.conn.execute(
            'SELECT id,identity_key FROM recordings WHERE id IN (?,?)', (previous[0], target[0]))}
        remaining = self.conn.execute('SELECT MIN(id) FROM catalog_entries WHERE recording_id=? AND video_id IS NOT ?',
            (previous[0], video_id)).fetchone()[0]
        return dict(previous=previous[0], target=target[0], previous_key=keys[previous[0]],
            target_key=keys[target[0]], remaining=remaining)

    @synchronized
    def record_association(self, video, previous, target, source, evidence_json, now):
        with self.conn.atomic(join=True):
            return self.conn.execute('INSERT INTO item_recording_associations(video_id,recording_id,previous_recording_id,'
                'source,evidence,at) VALUES (?,?,?,?,?,?)', (video, target, previous, source, evidence_json, now)).lastrowid

    @synchronized
    def assign_recording(self, video, previous, target, target_key):
        from yt_playlist.core import discovery_sources, embedding_files, listening_summaries
        with self.conn.atomic(join=True):
            self.conn.execute('DELETE FROM recording_embeddings WHERE recording_id IN (?,?)', (previous, target))
            if embedding_files.available(self.conn):
                self.conn.execute('DELETE FROM recording_embedding_files WHERE recording_id IN (?,?)', (previous, target))
            self.conn.execute('DELETE FROM prepared_recording_inputs WHERE recording_id IN (?,?)', (previous, target))
            discovery_sources.prepare_reassociation(self.conn, video, previous)
            self.conn.execute('UPDATE youtube_items SET recording_id=? WHERE video_id=?', (target, video))
            self.conn.execute('UPDATE catalog_entries SET recording_id=? WHERE video_id=?', (target, video))
            for owner in ('road_trip_draft_entries', 'radio_queue_items'):
                if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (owner,)).fetchone():
                    self.conn.execute(f'UPDATE {owner} SET recording_id=? WHERE video_id=?', (target, video))
            self.conn.execute('UPDATE legacy_track_refs SET recording_id=? WHERE video_id=?', (target, video))
            typed = listening_summaries.available(self.conn)
            assignment = 'unresolved_subject_key=NULL' if typed else 'identity_key=?'
            parameters = (target, video) if typed else (target, target_key, video)
            self.conn.execute(f'UPDATE main.play_events SET recording_id=?,{assignment} WHERE video_id=?', parameters)
            self.conn.execute('UPDATE recordings SET provisional=0 WHERE id=?', (target,))

    @synchronized
    def redirect_recording(self, previous, target, source, evidence_json):
        with self.conn.atomic(join=True):
            self.conn.execute('INSERT OR REPLACE INTO recording_redirects VALUES (?,?,?,?)', (previous, target, source, evidence_json))
            self.conn.execute('UPDATE recording_redirects SET recording_id=? WHERE recording_id=?', (target, previous))
            aliases = [dict(row) for row in self.conn.execute('SELECT alias,source FROM recording_aliases WHERE recording_id=?', (previous,))]
            self.conn.executemany('INSERT OR IGNORE INTO recording_aliases VALUES (?,?,?)',
                [(row['alias'], target, row['source']) for row in aliases])
            self.conn.execute('DELETE FROM recording_aliases WHERE recording_id=?', (previous,))
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
        row = self.conn.execute('SELECT * FROM catalog_entries WHERE video_id=?' if video_id else
            'SELECT * FROM catalog_entries WHERE source_ref=?', (video_id or source_ref,)).fetchone()
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
        if video_id:
            self.conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist) '
                'VALUES (?,?,?,?,?,?)', (video_id, recording, title, artist, title, artist))
        entry = self.conn.execute('INSERT INTO catalog_entries(recording_id,video_id,source_ref) VALUES (?,?,?)',
            (recording, video_id, source_ref)).lastrowid
        if not video_id:
            self.conn.execute('INSERT INTO unresolved_items(entry_id,title,artist,orig_title,orig_artist) VALUES (?,?,?,?,?)',
                (entry, title, artist, title, artist))
        return entry

    @synchronized
    def reconcile_admission(self, recording, video_id, now):
        from yt_playlist.core import listening_summaries
        with self.conn.atomic(join=True):
            key = self.conn.execute('SELECT identity_key FROM recordings WHERE id=?', (recording,)).fetchone()[0]
            typed = listening_summaries.available(self.conn)
            if video_id:
                self.conn.execute('INSERT INTO item_recording_associations(video_id,recording_id,source,evidence,at) '
                    "SELECT ?,?,'new_upload','{}',? WHERE NOT EXISTS(SELECT 1 FROM item_recording_associations WHERE video_id=?)",
                    (video_id, recording, now, video_id))
                assignment = 'unresolved_subject_key=NULL' if typed else 'identity_key=?'
                parameters = (recording, video_id) if typed else (recording, key, video_id)
                self.conn.execute(f'UPDATE main.play_events SET recording_id=?,{assignment} '
                    'WHERE video_id=? AND recording_id IS NULL', parameters)
            column = 'unresolved_subject_key' if typed else 'identity_key'
            self.conn.execute(f"UPDATE main.play_events SET {column}='@unresolved-video:'||video_id "
                f'WHERE recording_id IS NULL AND {column}=? AND NULLIF(trim(video_id),\'\') IS NOT NULL '
                'AND NOT EXISTS(SELECT 1 FROM youtube_items y WHERE y.video_id=play_events.video_id)', (key,))

    @snapshot_read
    def catalog_metadata_inputs(self, entry, fields):
        from yt_playlist.core import metadata_evidence
        return {'evidence': metadata_evidence.available(self.conn),
                'sources': {field: metadata.selected_source(self.conn, entry, field) for field in fields}}

    @synchronized
    def add_library_membership(self, entry, now):
        with self.conn.atomic(join=True):
            self.conn.execute("INSERT OR IGNORE INTO library_memberships VALUES (?,'observed',?)", (entry, now))

    @lookup_read
    def entry_key(self, entry):
        return self.conn.execute('SELECT identity_key FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0]

    @contextmanager
    def rating_transaction(self):
        """Keep the rating read and its prepared writes in this repository's unit of work."""
        with self._lock:
            with self.conn.atomic():
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
                    'SELECT y.video_id,f.created_at FROM rec_feedback f JOIN youtube_items y '
                    'ON y.recording_id=f.recording_id '
                    f"WHERE y.video_id IN ({slots}) AND f.surface='rating_override' AND f.kind='unban'", batch):
                unbanned.setdefault(row['video_id'], row['created_at'])
        return previous, unbanned

    @synchronized
    def write_item_ratings(self, observations, action_rows):
        with self.conn.atomic(join=True):
            ratings = entity_names.owner(self.conn, 'item_ratings')
            actions = entity_names.owner(self.conn, 'listening_rating_actions')
            self.conn.executemany(f'INSERT INTO {actions.table} '
                f'({actions.column("account_id")},video_id,previous_status,status,source,at) VALUES (?,?,?,?,?,?)',
                action_rows)
            self.conn.executemany(
                f'INSERT INTO {ratings.table} VALUES (?,?,?,?,?) '
                f'ON CONFLICT({ratings.column("account_id")},video_id) DO UPDATE SET '
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
        keys = sorted(set(keys) - {None})
        result = []
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset + 500]
            slots = ','.join('?' for _ in batch)
            result.extend(dict(row) for row in self.conn.execute(
                'SELECT r.identity_key,ir.status,ir.source,ir.observed_at,'
                'EXISTS (SELECT 1 FROM rec_feedback f WHERE f.item_key=r.identity_key '
                "AND f.surface='rating_override' AND f.kind='unban') unbanned,"
                '(SELECT MAX(f.created_at) FROM rec_feedback f WHERE f.item_key=r.identity_key '
                "AND f.surface='rating_override' AND f.kind='unban') unbanned_at "
                'FROM item_ratings ir JOIN youtube_items y ON y.video_id=ir.video_id '
                f'JOIN recordings r ON r.id=y.recording_id WHERE r.identity_key IN ({slots})', batch))
        return result

    @snapshot_read
    def action_rating_rows(self, account_id):
        return [dict(row) for row in self.conn.execute(
            "SELECT ir.video_id,ir.status,ir.observed_at,r.identity_key FROM item_ratings ir "
            "LEFT JOIN youtube_items y ON y.video_id=ir.video_id "
            "LEFT JOIN recordings r ON r.id=y.recording_id "
            "WHERE ir.account_id=? AND ir.source IN ('api','action') "
            "ORDER BY ir.observed_at,ir.video_id", (account_id,))]

    @snapshot_read
    def rating_feedback_rows(self, keys):
        from yt_playlist.core.rating_feedback import feedback_rows

        return feedback_rows(self.conn, keys)

    @lookup_read
    def identity_key_for_video(self, video_id) -> str | None:
        """Resolve an observed upload in the catalog, independently of library membership."""
        row = self.conn.execute(
            "SELECT identity_key FROM catalog_tracks WHERE video_id=? LIMIT 1", (video_id,)).fetchone()
        return row["identity_key"] if row else None

    @lookup_read
    def library_identity_key_for_video(self, video_id) -> str | None:
        """Resolve a library upload without treating cached observations as membership."""
        row = self.conn.execute(
            "SELECT identity_key FROM tracks WHERE video_id=? LIMIT 1", (video_id,)).fetchone()
        return row['identity_key'] if row else None

    @snapshot_read
    def identity_keys_for_videos(self, video_ids) -> dict:
        """Resolve a bounded set of uploads without reopening the catalog per video."""
        requested = list(dict.fromkeys(video_ids))
        result = {}
        for offset in range(0, len(requested), 500):
            page = requested[offset:offset + 500]
            placeholders = ','.join('?' for _ in page)
            for row in self.conn.execute(
                    f'SELECT video_id,identity_key FROM catalog_tracks WHERE video_id IN ({placeholders})', page):
                result.setdefault(row['video_id'], row['identity_key'])
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
              "OR t.bpm IS NULL OR t.energy IS NULL OR t.danceability IS NULL "
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
        track_id = catalog.resolve_entry(self.conn, track_id)
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
            f"JOIN {self.album_track_links_table} a ON a.entry_id=t.id "
            f"WHERE a.browse_id=? AND ({self._NEEDS}) ORDER BY t.id", (album_browse_id,)).fetchall()
        return [dict(r) for r in rows]

    @snapshot_read
    def artwork_inputs(self, track_id):
        track_id = catalog.resolve_entry(self.conn, track_id)
        row = self.conn.execute(
            "SELECT id,thumbnail,EXISTS (SELECT 1 FROM entry_field_choices c "
            "WHERE c.entry_id=t.id AND c.field='thumbnail') chosen "
            'FROM catalog_tracks t WHERE id=?', (track_id,)).fetchone()
        return dict(row) if row is not None else None

    @synchronized
    def artwork_source(self, track_id):
        """Current artwork origin; an explicit choice always belongs to the user."""
        track_id = catalog.resolve_entry(self.conn, track_id)
        if self.conn.execute("SELECT 1 FROM entry_field_choices WHERE entry_id=? AND field='thumbnail'",
                             (track_id,)).fetchone():
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
        track_id = catalog.resolve_entry(self.conn, track_id)
        row = self.conn.execute('SELECT id,orig_title,orig_artist FROM catalog_tracks WHERE id=?', (track_id,)).fetchone()
        return dict(row) if row is not None else None

    @synchronized
    def get_track_enrichment(self, track_id):
        track_id = catalog.resolve_entry(self.conn, track_id)
        """Current (genre, year) for a track, used to report the effective value after a fill."""
        row = self.conn.execute("SELECT genre, mb_year FROM catalog_tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            return ("", "")
        return (row["genre"] or "", row["mb_year"] or "")

    @synchronized
    def get_track_audio(self, track_id):
        track_id = catalog.resolve_entry(self.conn, track_id)
        """Current (bpm, energy, danceability) for a track, each float or None."""
        row = self.conn.execute(
            "SELECT bpm, energy, danceability FROM catalog_tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            return (None, None, None)
        return (row["bpm"], row["energy"], row["danceability"])

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
            f"SELECT DISTINCT a.browse_id FROM {self.album_track_links_table} a JOIN library_memberships m ON m.entry_id=a.entry_id")}
