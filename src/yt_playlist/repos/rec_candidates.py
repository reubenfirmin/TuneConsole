"""Ranked, playable library candidate reads.

These queries preserve SQL ordering and bounded limits. Surface composition,
personalized scoring and lane selection remain in rec.surfaces above persistence.
"""
from yt_playlist.repos.base import LIKED_EXISTS, Repo, synchronized
from yt_playlist.repos.rec_eligibility import exclude_ids


class RecCandidatesRepo(Repo):
    @synchronized
    def revisit_candidates(self, now, *, keys=None, limit=90):
        """Quiet liked/repeated songs, including actual generated-source listening.

        Exact events count as plays; calendar-only imports contribute once on days
        without an exact event. Membership in a generated playlist has no effect.
        Twenty recorded plays gives a song the longer, six-month rest.
        """
        params = {'month': now - 30*86400, 'half_year': now - 183*86400, 'limit': limit}
        restriction = ''
        if keys is not None:
            keys = list(dict.fromkeys(keys))
            if not keys:
                return []
            names = []
            for i, key in enumerate(keys):
                params[f'key{i}'] = key
                names.append(f':key{i}')
            restriction = ' AND t.identity_key IN (' + ','.join(names) + ')'
        rows = self.conn.execute(
            'WITH names AS (SELECT t.identity_key key,MIN(t.title) title,MIN(t.artist) artist,'
            'MIN(t.album) album,MIN(t.video_id) video_id,MIN(t.thumbnail) thumbnail,'
            f'MAX(CASE WHEN {LIKED_EXISTS} THEN 1 ELSE 0 END) liked '
            'FROM playable_library_tracks t WHERE t.video_id IS NOT NULL AND t.video_id<>\'\' '
            f'AND {self._store.rec_eligibility.track_length_clause("t.duration_s")}{restriction} '
            'GROUP BY t.identity_key), heard AS ('
            'SELECT pe.identity_key,pe.played_at at FROM play_events pe JOIN names n ON n.key=pe.identity_key '
            'UNION ALL SELECT d.identity_key,d.day_ts+86400 FROM listening_days d '
            'JOIN names n ON n.key=d.identity_key WHERE NOT EXISTS ('
            'SELECT 1 FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id '
            'WHERE pe.identity_key=d.identity_key AND pe.identity_id=d.identity_id AND c.day=d.day)), '
            'totals AS (SELECT identity_key,COUNT(*) plays,MAX(at) last_played FROM heard GROUP BY identity_key) '
            'SELECT n.*,COALESCE(h.plays,0) plays,h.last_played FROM names n '
            'LEFT JOIN totals h ON h.identity_key=n.key '
            'WHERE (n.liked=1 OR h.plays>=2) AND (h.last_played IS NULL OR '
            'h.last_played<=CASE WHEN h.plays>=20 THEN :half_year ELSE :month END) '
            'ORDER BY COALESCE(h.plays,0),h.last_played,n.key LIMIT :limit', params).fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def comfort_candidates(self, now, min_plays=4, recency_full_days=30, limit=50) -> list[dict]:
        """High-rotation favorites, demoted by how recently they were heard."""
        full_secs = max(1.0, recency_full_days * 86400.0)
        rows = self.conn.execute(
            "WITH plays AS (SELECT hi.identity_key,COUNT(*) c,MAX(hi.day_ts) last "
            "  FROM listening_days hi "
            "  GROUP BY hi.identity_key), "
            "     names AS (SELECT identity_key, MIN(title) title, MIN(artist) artist, "
            "               MIN(album) album, MIN(video_id) vid, MIN(thumbnail) thumb "
            f"              FROM playable_library_tracks WHERE {self._store.rec_eligibility.track_length_clause()} GROUP BY identity_key) "
            "SELECT n.identity_key k, n.title, n.artist, n.album, n.vid, n.thumb, "
            "       p.c plays, p.last last "
            "FROM names n JOIN plays p ON p.identity_key=n.identity_key "
            "WHERE n.title <> '' AND p.c >= :min_plays "
            "ORDER BY p.c * min(1.0, (:now - p.last) / :full_secs) DESC, p.last ASC LIMIT :limit",
            {"min_plays": min_plays, "now": now, "full_secs": full_secs, "limit": limit}).fetchall()
        return [{"key": r["k"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "last_played": r["last"]} for r in rows]

    @synchronized
    def more_like_rotation(self, seed_limit=40, limit=40) -> list[dict]:
        """Barely played co-members of playlists containing the most-played seeds."""
        excl = self._store.rec_eligibility.excluded_playlist_ids()
        seed_excl = exclude_ids("pt.playlist_id", excl)
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM listening_days GROUP BY identity_key), "
            " seeds AS (SELECT identity_key k FROM tp ORDER BY c DESC LIMIT :seed_limit), "
            " seedpl AS (SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            "            JOIN tracks t ON t.id=pt.track_id JOIN seeds s ON s.k=t.identity_key"
            f"            WHERE 1=1{seed_excl}), "
            " cand AS (SELECT t.identity_key key, MIN(c.title) title, MIN(c.artist) artist, "
            "                 MIN(c.album) album, MIN(c.video_id) vid, MIN(c.thumbnail) thumb, "
            "                 COUNT(DISTINCT pt.playlist_id) sp, COALESCE(MAX(tp.c),0) plays "
            "          FROM playlist_tracks pt JOIN seedpl ON seedpl.pid=pt.playlist_id "
            "          JOIN tracks t ON t.id=pt.track_id "
            "          JOIN playable_library_tracks c ON c.recording_id=t.recording_id "
            "          LEFT JOIN tp ON tp.identity_key=t.identity_key "
            f"          WHERE c.title<>'' AND {self._store.rec_eligibility.track_length_clause('c.duration_s')} GROUP BY t.identity_key) "
            "SELECT key, title, artist, album, vid, thumb, sp, plays FROM cand "
            "WHERE key NOT IN (SELECT k FROM seeds) AND plays<=1 "
            "ORDER BY sp DESC, plays ASC, key LIMIT :limit",
            {"seed_limit": seed_limit, "limit": limit}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "shared_playlists": r["sp"]} for r in rows]

    @synchronized
    def deep_cuts(self, limit=40) -> list[dict]:
        """Each heavily played artist's least-played track, ordered by affinity."""
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM listening_days GROUP BY identity_key), "
            " trk AS (SELECT t.identity_key key, MIN(t.title) title, MIN(t.artist) artist, "
            "                MIN(t.album) album, MIN(t.video_id) vid, MIN(t.thumbnail) thumb, "
            "                COALESCE(MAX(tp.c),0) plays "
            "         FROM playable_library_tracks t LEFT JOIN tp ON tp.identity_key=t.identity_key "
            f"        WHERE t.title<>'' AND t.artist<>'' AND {self._store.rec_eligibility.track_length_clause('t.duration_s')} "
            "         GROUP BY t.identity_key), "
            " ap AS (SELECT artist, SUM(plays) total FROM trk GROUP BY artist), "
            " r AS (SELECT trk.*, ap.total atot, "
            "        ROW_NUMBER() OVER (PARTITION BY trk.artist ORDER BY trk.plays ASC, trk.key) rn "
            "       FROM trk JOIN ap ON ap.artist=trk.artist WHERE ap.total>0) "
            "SELECT key, title, artist, album, vid, thumb, plays, atot FROM r WHERE rn=1 "
            "ORDER BY atot DESC, plays ASC, key LIMIT :limit",
            {"limit": limit}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "artist_plays": r["atot"]} for r in rows]

    @synchronized
    def complete_playlist(self, playlist_id, limit=20) -> list[dict]:
        """Owned tracks fitting a playlist, ranked by artist and co-occurrence."""
        rows = self.conn.execute(
            "WITH pm AS (SELECT t.identity_key key, t.artist FROM playlist_tracks pt "
            "            JOIN tracks t ON t.id=pt.track_id WHERE pt.playlist_id=:pid), "
            " pa AS (SELECT DISTINCT artist FROM pm WHERE artist<>''), "
            " shared AS (SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            "            JOIN tracks t ON t.id=pt.track_id JOIN pm ON pm.key=t.identity_key "
            "            WHERE pt.playlist_id<>:pid), "
            " cand AS (SELECT t.identity_key key, MIN(c.title) title, MIN(c.artist) artist, "
            "                 MIN(c.album) album, MIN(c.video_id) vid, MIN(c.thumbnail) thumb, "
            "                 MAX(CASE WHEN t.artist IN (SELECT artist FROM pa) THEN 1 ELSE 0 END) sa, "
            "                 COUNT(DISTINCT CASE WHEN pt.playlist_id IN (SELECT pid FROM shared) "
            "                                THEN pt.playlist_id END) cooc "
            "          FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id "
            "          JOIN playable_library_tracks c ON c.recording_id=t.recording_id "
            f"          WHERE t.identity_key NOT IN (SELECT key FROM pm) AND c.title<>'' "
            f"          AND {self._store.rec_eligibility.track_length_clause('c.duration_s')} "
            "          GROUP BY t.identity_key) "
            "SELECT key, title, artist, album, vid, thumb, sa, cooc FROM cand "
            "WHERE sa=1 OR cooc>0 ORDER BY (sa*2+cooc) DESC, cooc DESC, key LIMIT :limit",
            {"pid": playlist_id, "limit": limit}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"],
                 "same_artist": bool(r["sa"]), "cooc": r["cooc"]} for r in rows]
