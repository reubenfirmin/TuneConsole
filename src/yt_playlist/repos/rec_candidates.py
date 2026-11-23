"""Ranked, playable library candidate reads.

These queries preserve SQL ordering and bounded limits. Surface composition,
personalized scoring and lane selection remain in rec.surfaces above persistence.
"""
from yt_playlist.core import recording_library
from yt_playlist.core.catalog_query import LIBRARY_TRACKS_SQL, playable_tracks_sql
from yt_playlist.library.rating_policy import liked_music_selection
from yt_playlist.repos.base import Repo, snapshot_read
from yt_playlist.repos.rec_eligibility import exclude_ids, duration_clause


class RecCandidatesRepo(Repo):
    @snapshot_read
    def revisit_candidates(self, criteria, *, maximum_duration, keys=None, limit=90):
        """Quiet liked/repeated songs, including actual generated-source listening.

        Exact events count as plays; calendar-only imports contribute once on days
        without an exact event. Membership in a generated playlist has no effect.
        Twenty recorded plays gives a song the longer, six-month rest.
        """
        return self._quiet_favorites(criteria, maximum_duration=maximum_duration, keys=keys,
                                     order='COALESCE(h.plays,0),h.last_played,n.key', limit=limit)

    def _quiet_favorites(self, criteria, *, maximum_duration, keys, order, limit, scoped_listening=False):
        restriction = ''
        if keys is not None:
            keys = tuple(dict.fromkeys(keys))
            if not keys:
                return []
            restriction = ' AND t.identity_key IN (' + ','.join('?' for _ in keys) + ')'
        # Reconcile only the requested songs' days. Installed identity views
        # compute these keys: casting the materialized key gives SQLite the
        # affinity it needs to index this lookup for the calendar-only branch.
        library = recording_library.TRACKS_SQL
        eligible = (f'SELECT t.* FROM ({library}) t '
                    f'WHERE {duration_clause("t.duration_s", maximum_duration)}{restriction}')
        descriptions = ('MIN(t.title) title,MIN(t.artist) artist,MIN(t.album) album,'
                        'MIN(t.video_id) video_id,MIN(t.thumbnail) thumbnail,') if criteria else ''
        display = 'n.title,n.artist,n.album,n.video_id,n.thumbnail,' if criteria else ''
        filters = ('WHERE (n.liked=1 OR h.plays>=?) AND (h.last_played IS NULL OR '
                   'h.last_played<=CASE WHEN h.plays>=? THEN ? ELSE ? END) '
                   f'ORDER BY {order} LIMIT ?') if criteria else ''
        cutoffs = (criteria.repeated_plays, criteria.long_rest_plays, criteria.half_year,
                   criteria.month, limit) if criteria else ()
        if scoped_listening:
            # Probe only the prepared recordings and their unresolved aliases.
            exact_plays = ('SELECT n.key,p.id,p.account_id,p.played_at FROM names n '
                'CROSS JOIN play_events p ON p.recording_id=n.recording_id '
                'UNION ALL SELECT n.key,p.id,p.account_id,p.played_at FROM names n '
                'CROSS JOIN play_events p ON p.recording_id IS NULL AND p.unresolved_subject_key=n.key')
            # Exact plays already count, including generated plays. Any date
            # left over can only come from a calendar observation.
            calendar_days = ('SELECT n.key identity_key,o.account_id,o.day FROM names n '
                'CROSS JOIN listening_observations o ON o.recording_id=n.recording_id WHERE o.day IS NOT NULL '
                'UNION SELECT n.key,o.account_id,o.day FROM names n CROSS JOIN listening_observations o '
                'ON o.recording_id IS NULL AND o.unresolved_subject_key=n.key WHERE o.day IS NOT NULL')
        else:
            exact_plays = ('SELECT pe.identity_key key,pe.id,pe.account_id,pe.played_at '
                           'FROM play_event_subjects pe WHERE pe.identity_key IN (SELECT key FROM names)')
            calendar_days = 'SELECT identity_key,account_id,day FROM listening_days WHERE identity_key IN (SELECT key FROM names)'
        with liked_music_selection(self._store) as (liked, playlist_ids):
            rows = self.conn.execute(
                'WITH names AS MATERIALIZED (SELECT t.identity_key key,MIN(t.recording_id) recording_id,'
                f'{descriptions}'
                f'MAX(CASE WHEN {liked} THEN 1 ELSE 0 END) liked '
                f'FROM ({playable_tracks_sql(eligible)}) t '
                'GROUP BY t.identity_key), '
                f'exact_plays AS MATERIALIZED ({exact_plays}), '
                'exact_days AS MATERIALIZED (SELECT CAST(pe.key AS TEXT) identity_key,pe.account_id,c.day '
                'FROM exact_plays pe JOIN play_event_calendar c ON c.event_id=pe.id), '
                f'calendar_days AS ({calendar_days}), heard AS ('
                'SELECT pe.key identity_key,pe.played_at at FROM exact_plays pe '
                "UNION ALL SELECT d.identity_key,CAST(strftime('%s',d.day||' 12:00:00') AS REAL)+86400 "
                'FROM calendar_days d LEFT JOIN exact_days pe ON pe.identity_key=d.identity_key '
                'AND pe.account_id=d.account_id AND pe.day=d.day WHERE pe.identity_key IS NULL), '
                'totals AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key, '
                'COUNT(*) plays,MAX(at) last_played FROM heard GROUP BY identity_key) '
                f'SELECT n.key,{display}n.liked,COALESCE(h.plays,0) plays,'
                'h.last_played FROM names n '
                'LEFT JOIN totals h ON h.identity_key=n.key '
                f'{filters}', (*playlist_ids, *(keys or ()), *cutoffs)).fetchall()
        return [dict(row) for row in rows]

    @snapshot_read
    def comfort_candidates(self, criteria, *, maximum_duration, order, keys=None, limit=50) -> list[dict]:
        """Liked or repeatedly heard favorites with no listening during their rest."""
        return self._quiet_favorites(criteria, maximum_duration=maximum_duration,
                                     keys=keys, order=order, limit=limit, scoped_listening=keys is not None)

    @snapshot_read
    def comfort_facts(self, *, maximum_duration, keys):
        return self._quiet_favorites(None, maximum_duration=maximum_duration, keys=keys,
                                     order=None, limit=None, scoped_listening=True)

    @snapshot_read
    def more_like_rotation(self, *, seed_limit, limit, maximum_plays, maximum_duration, order,
                           excluded_playlists) -> list[dict]:
        """Barely played co-members of playlists containing the most-played seeds."""
        seed_excl = exclude_ids('pt.playlist_id', excluded_playlists)
        eligible = f"SELECT * FROM ({LIBRARY_TRACKS_SQL}) c WHERE c.title<>'' AND {duration_clause('c.duration_s', maximum_duration)}"
        rows = self.conn.execute(
            "WITH tp AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key, "
            "COUNT(*) c FROM listening_days GROUP BY identity_key), "
            " seeds AS (SELECT identity_key k FROM tp ORDER BY c DESC LIMIT :seed_limit), "
            " seedpl AS (SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            "            JOIN catalog_tracks t ON t.id=pt.track_id JOIN seeds s ON s.k=t.identity_key"
            f"            WHERE 1=1{seed_excl}), "
            f" representatives AS MATERIALIZED ({playable_tracks_sql(eligible)}), "
            " cand AS (SELECT t.identity_key key, MIN(c.title) title, MIN(c.artist) artist, "
            "                 MIN(c.album) album, MIN(c.video_id) vid, MIN(c.thumbnail) thumb, "
            "                 COUNT(DISTINCT pt.playlist_id) sp, COALESCE(MAX(tp.c),0) plays "
            "          FROM playlist_tracks pt JOIN seedpl ON seedpl.pid=pt.playlist_id "
            "          JOIN catalog_tracks t ON t.id=pt.track_id "
            "          JOIN representatives c ON c.recording_id=t.recording_id "
            "          LEFT JOIN tp ON tp.identity_key=t.identity_key "
            "          GROUP BY t.identity_key) "
            "SELECT key, title, artist, album, vid, thumb, sp, plays FROM cand "
            'WHERE key NOT IN (SELECT k FROM seeds) AND plays<=:maximum_plays '
            f'ORDER BY {order} LIMIT :limit',
            {'seed_limit': seed_limit, 'limit': limit, 'maximum_plays': maximum_plays}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "shared_playlists": r["sp"]} for r in rows]

    @snapshot_read
    def deep_cuts(self, *, maximum_duration, minimum_artist_plays, order, limit=40) -> list[dict]:
        """Each heavily played artist's least-played track, ordered by affinity."""
        eligible = (f"SELECT * FROM ({LIBRARY_TRACKS_SQL}) t WHERE t.title<>'' AND t.artist<>'' "
                    f"AND {duration_clause('t.duration_s', maximum_duration)}")
        rows = self.conn.execute(
            "WITH tp AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key, "
            "COUNT(*) c FROM listening_days GROUP BY identity_key), "
            " trk AS (SELECT t.identity_key key, MIN(t.title) title, MIN(t.artist) artist, "
            "                MIN(t.album) album, MIN(t.video_id) vid, MIN(t.thumbnail) thumb, "
            "                COALESCE(MAX(tp.c),0) plays "
            f"         FROM ({playable_tracks_sql(eligible)}) t LEFT JOIN tp ON tp.identity_key=t.identity_key "
            "         GROUP BY t.identity_key), "
            " ap AS (SELECT artist, SUM(plays) total FROM trk GROUP BY artist), "
            " r AS (SELECT trk.*, ap.total atot, "
            "        ROW_NUMBER() OVER (PARTITION BY trk.artist ORDER BY trk.plays ASC, trk.key) rn "
            '       FROM trk JOIN ap ON ap.artist=trk.artist WHERE ap.total>:minimum_artist_plays) '
            "SELECT key, title, artist, album, vid, thumb, plays, atot FROM r WHERE rn=1 "
            f'ORDER BY {order} LIMIT :limit',
            {'limit': limit, 'minimum_artist_plays': minimum_artist_plays}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "artist_plays": r["atot"]} for r in rows]

    @snapshot_read
    def complete_playlist(self, playlist_id, *, maximum_duration, artist_weight, order, limit=20) -> list[dict]:
        """Owned tracks fitting a playlist, ranked by artist and co-occurrence."""
        eligible = f"SELECT * FROM ({LIBRARY_TRACKS_SQL}) c WHERE c.title<>'' AND {duration_clause('c.duration_s', maximum_duration)}"
        rows = self.conn.execute(
            "WITH pm AS (SELECT t.identity_key key, t.artist FROM playlist_tracks pt "
            "            JOIN catalog_tracks t ON t.id=pt.track_id WHERE pt.playlist_id=:pid), "
            " pa AS (SELECT DISTINCT artist FROM pm WHERE artist<>''), "
            " shared AS (SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            "            JOIN catalog_tracks t ON t.id=pt.track_id JOIN pm ON pm.key=t.identity_key "
            "            WHERE pt.playlist_id<>:pid), "
            f" representatives AS MATERIALIZED ({playable_tracks_sql(eligible)}), "
            " cand AS (SELECT t.identity_key key, MIN(c.title) title, MIN(c.artist) artist, "
            "                 MIN(c.album) album, MIN(c.video_id) vid, MIN(c.thumbnail) thumb, "
            "                 MAX(CASE WHEN t.artist IN (SELECT artist FROM pa) THEN 1 ELSE 0 END) sa, "
            "                 COUNT(DISTINCT CASE WHEN pt.playlist_id IN (SELECT pid FROM shared) "
            "                                THEN pt.playlist_id END) cooc "
            "          FROM catalog_tracks t JOIN playlist_tracks pt ON pt.track_id=t.id "
            "          JOIN representatives c ON c.recording_id=t.recording_id "
            "          WHERE t.identity_key NOT IN (SELECT key FROM pm) "
            "          GROUP BY t.identity_key) "
            "SELECT key, title, artist, album, vid, thumb, sa, cooc FROM cand "
            f'WHERE sa=1 OR cooc>0 ORDER BY {order} LIMIT :limit',
            {'pid': playlist_id, 'limit': limit, 'artist_weight': artist_weight}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"],
                 "same_artist": bool(r["sa"]), "cooc": r["cooc"]} for r in rows]
