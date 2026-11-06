"""ChartsRepo: play-history statistics for the charts / artist / playlist UI pages
(most-played songs and artists, per-playlist listen stats, and per-track detail views).
"""
from contextlib import contextmanager

from yt_playlist.library.rating_policy import LIKED_EXISTS
from yt_playlist.repos.base import Repo, synchronized, snapshot_read
from yt_playlist.core import artists
from yt_playlist.core.catalog_query import playable_tracks_sql

# Per-identity category expression for the non-playlist ticker dimensions. Each picks one
# representative value per identity_key (dup uploads of the same song collapse to one), and
# yields NULL for untagged songs so they drop out of the distribution rather than bucketing
# as ''. `year` floors the 4-digit mb_year to its decade ("1995" -> "1990").
_CAT_EXPR = {
    "genre": "MIN(NULLIF(genre,''))",
    "artist": "MIN(NULLIF(artist,''))",
    # year: take the first 4 chars of mb_year (substr ...,1,4); GLOB '[0-9]x4' gates it to a real
    # 4-digit year (else CASE yields NULL -> dropped); CAST to INTEGER, //10*10 floors to the decade,
    # CAST back to TEXT so it shares the string category column with the other dimensions.
    "year": ("CASE WHEN substr(MIN(NULLIF(mb_year,'')),1,4) GLOB '[0-9][0-9][0-9][0-9]' "
             "THEN CAST(CAST(substr(MIN(NULLIF(mb_year,'')),1,4) AS INTEGER)/10*10 AS TEXT) "
             "END"),
}
# Distinct (song, playlist) membership: dedups dup track rows so a play counts once per playlist.
_PL_MEMBERSHIP = ("SELECT DISTINCT t.identity_key ik, pt.playlist_id pid "
                  "FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id")
_ALBUM_MEMBERSHIP = ("SELECT DISTINCT t.identity_key ik,at.album_id aid "
                     "FROM tracks t JOIN {tracks} at ON at.entry_id=t.id "
                     "JOIN album_primary_provider_ids p ON p.album_id=at.album_id AND p.provider='youtube'")
# The listening-day projection combines evidence sources. Evaluate it once,
# giving its computed identity key the affinity needed for indexed lookups.
_PLAY_COUNTS_CTE = ("play_counts AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key,"
                    "COUNT(*) plays FROM listening_days GROUP BY identity_key)")
_TRACK_PLAYS = "COALESCE((SELECT plays FROM play_counts hi WHERE hi.identity_key=t.identity_key),0)"


class ChartsRepo(Repo):
    @snapshot_read
    def ticker_thumbnails(self, dimension) -> dict:
        """Local artwork for chart categories, in one query per dimension."""
        if dimension == "artist":
            rows = self.conn.execute(
                "SELECT artist cat, MIN(NULLIF(thumbnail,'')) thumbnail FROM tracks "
                "WHERE artist<>'' GROUP BY artist").fetchall()
        elif dimension == "album":
            rows = self.conn.execute(
                "SELECT '@album:'||a.id cat,COALESCE(NULLIF(a.thumbnail,''),MIN(NULLIF(t.thumbnail,''))) thumbnail "
                f"FROM albums a JOIN {self.album_tracks_table} at ON at.album_id=a.id JOIN tracks t ON t.id=at.entry_id "
                "JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
                "GROUP BY a.id").fetchall()
        elif dimension == "playlist":
            rows = self.conn.execute(
                "SELECT '@playlist:'||p.id cat, COALESCE(MIN(NULLIF(p.thumbnail,'')), "
                "MIN(NULLIF(t.thumbnail,''))) thumbnail FROM playlists p "
                "LEFT JOIN playlist_tracks pt ON pt.playlist_id=p.id "
                "LEFT JOIN tracks t ON t.id=pt.track_id GROUP BY p.id").fetchall()
        else:
            raise ValueError(f"No ticker artwork for dimension: {dimension}")
        return {r["cat"]: r["thumbnail"] for r in rows if r["thumbnail"]}

    @synchronized
    def album_browse_ids(self) -> dict:
        """Chart release keys to their exact YouTube browse IDs."""
        rows = self.conn.execute(
            "SELECT '@album:'||a.id k,p.provider_id b FROM albums a JOIN album_primary_provider_ids p "
            "ON p.album_id=a.id AND p.provider='youtube' WHERE EXISTS "
            f"(SELECT 1 FROM {self.album_tracks_table} at JOIN tracks t ON t.id=at.entry_id WHERE at.album_id=a.id)").fetchall()
        return {r['k']:r['b'] for r in rows}

    @synchronized
    def album_labels(self):
        return {f'@album:{r[0]}':r[1] for r in self.conn.execute(
            "SELECT a.id,a.title FROM albums a JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube'")}

    @synchronized
    def playlist_labels(self):
        """Playlist titles are presentation labels; local IDs retain account scope."""
        return {f'@playlist:{r[0]}': r[1] for r in self.conn.execute(
            "SELECT id,title FROM playlists")}

    @snapshot_read
    def history_bounds(self) -> tuple:
        """(earliest, latest) snapshot taken_at across all sync history, or (None, None) if empty.
        Lets the ticker size its candle periods to how much history actually exists."""
        r = self.conn.execute(
            "SELECT MIN(day_ts) lo,MAX(day_ts) hi FROM listening_days").fetchone()
        if r is None or r["lo"] is None:
            return (None, None)
        return (r["lo"], r["hi"])

    @snapshot_read
    def corpus_distribution(self, dimension) -> dict:
        """Library composition for a ticker dimension: {category: song_count}, counted per
        distinct identity_key (one song = one unit) so it's comparable with listen_distribution.

        dimension: 'genre' | 'year' | 'album' | 'playlist'. Untagged songs are excluded.
        Playlist counts a song once per playlist it belongs to (memberships sum > #songs).
        """
        if dimension == "playlist":
            rows = self.conn.execute(
                f"WITH pl AS ({_PL_MEMBERSHIP}) "
                "SELECT '@playlist:'||p.id cat, COUNT(DISTINCT pl.ik) c "
                "FROM pl JOIN playlists p ON p.id=pl.pid GROUP BY p.id").fetchall()
        elif dimension == 'album':
            rows = self.conn.execute(
                f"WITH membership AS ({_ALBUM_MEMBERSHIP.format(tracks=self.album_tracks_table)}) "
                "SELECT '@album:'||aid cat,COUNT(*) c FROM membership GROUP BY aid").fetchall()
        else:
            rows = self.conn.execute(
                f"WITH ident AS (SELECT identity_key, {_CAT_EXPR[dimension]} cat "
                "FROM tracks GROUP BY identity_key) "
                "SELECT cat, COUNT(*) c FROM ident WHERE cat IS NOT NULL GROUP BY cat").fetchall()
        return {r["cat"]: r["c"] for r in rows}

    @snapshot_read
    def listen_distribution(self, dimension, since=None, until=None) -> dict:
        """Plays per category in a time window: {category: play_count}, a "play" = one history-item
        appearance whose snapshot taken_at is in [since, until) (None bound = open that side).
        Same dimensions/units as corpus_distribution, so shares of the two are directly comparable.
        Disjoint [since, until) periods give the ticker candle its open/close/high/low.
        """
        bounds = "(:since IS NULL OR hi.day_ts>=:since) AND (:until IS NULL OR hi.day_ts<:until)"
        args = {"since": since, "until": until}
        if dimension == "playlist":
            rows = self.conn.execute(
                f"WITH pl AS ({_PL_MEMBERSHIP}) "
                "SELECT '@playlist:'||p.id cat, COUNT(*) c "
                "FROM listening_days hi "
                "JOIN pl ON pl.ik=hi.identity_key JOIN playlists p ON p.id=pl.pid "
                f"WHERE {bounds} GROUP BY p.id", args).fetchall()
        elif dimension == 'album':
            rows = self.conn.execute(
                f"WITH membership AS ({_ALBUM_MEMBERSHIP.format(tracks=self.album_tracks_table)}) "
                "SELECT '@album:'||m.aid cat,COUNT(*) c FROM listening_days hi "
                "JOIN membership m ON m.ik=hi.identity_key "
                f"WHERE {bounds} GROUP BY m.aid",args).fetchall()
        else:
            rows = self.conn.execute(
                f"WITH ident AS (SELECT identity_key, {_CAT_EXPR[dimension]} cat "
                "FROM tracks GROUP BY identity_key) "
                "SELECT i.cat, COUNT(*) c "
                "FROM listening_days hi "
                "JOIN ident i ON i.identity_key=hi.identity_key "
                f"WHERE i.cat IS NOT NULL AND {bounds} "
                "GROUP BY i.cat", args).fetchall()
        return {r["cat"]: r["c"] for r in rows}

    @snapshot_read
    def get_playlist_listen_stats(self) -> dict:
        """Descriptive activity of member songs, wherever they were played.

        Recording membership is deduplicated before joining evidence, so another
        confirmed upload changes playback membership without multiplying taste weight.
        This is not evidence of playing a playlist; use get_playlist_playback_stats
        for the playlist's own Last played and listening-session count.
        """
        rows = self.conn.execute(
            f"WITH membership AS ({_PL_MEMBERSHIP}) "
            "SELECT m.pid,COUNT(*) cnt,MAX(a.at) last FROM membership m "
            "JOIN listening_activity a ON a.identity_key=m.ik GROUP BY m.pid").fetchall()
        return {r["pid"]: (r["last"], r["cnt"]) for r in rows}

    @snapshot_read
    def get_playlist_playback_stats(self, *, policy) -> dict:
        """Actual playlist listening: {pid: (last_observed_playback, sessions)}.

        The caller supplies observation, normalization and session rules. The
        query resolves account-scoped playlist references and aggregates them.
        """
        rows = self.conn.execute(f"""
            WITH observations AS (
                {policy.observations}
            ), normalized AS (
                SELECT account_id,at,{policy.context} context FROM observations
            ), playlist_contexts AS MATERIALIZED (
                -- Index the two accepted context keys once. An OR in the
                -- join scans every playlist in the account per observation.
                -- UNION also keeps a matching pair of keys from duplicating
                -- the same playlist observation.
                SELECT account_id,CAST(provider_playlist_id AS TEXT) context,id,created_at FROM playlists
                UNION
                SELECT account_id,CAST(playlist_scope_id AS TEXT),id,created_at FROM playlists
            ), attributed AS (
                SELECT o.account_id,o.at,p.id pid FROM normalized o
                LEFT JOIN playlist_contexts p ON p.account_id=o.account_id AND p.context=o.context
                    AND (p.created_at IS NULL OR o.at>=p.created_at)
            ), ordered AS (
                SELECT *,LAG(pid) OVER sequence previous,
                    LAG(at) OVER sequence previous_at FROM attributed
                WINDOW sequence AS (PARTITION BY account_id ORDER BY at,pid)
            )
            SELECT pid,MAX(at) last,SUM(CASE WHEN {policy.starts_session} THEN 1 ELSE 0 END) sessions
            FROM ordered WHERE pid IS NOT NULL GROUP BY pid
        """, {'gap_seconds': policy.gap_seconds}).fetchall()
        return {r['pid']: (r['last'], r['sessions']) for r in rows}

    @snapshot_read
    def get_playlist_track_recency(self, *, include_generated=False, playlist_id=None) -> dict:
        """Per-playlist {playlist_id: [per-track last-played ts | None, ...]}, one entry per distinct
        track, its newest snapshot (None = never played). Unlike get_playlist_listen_stats (which
        collapses to the single freshest track), this keeps every track's recency so callers can judge
        a playlist by the *aggregate* staleness of its tracks, not its one most-recently-played song.
        Collection includes generated plays; recommendation callers retain organic history only.
        A target ID limits membership for fresh per-playlist deletion checks.
        """
        activity = ('SELECT identity_key,at FROM listening_activity UNION ALL '
                    'SELECT identity_key,played_at at FROM play_event_subjects WHERE is_generated=1'
                    if include_generated else 'SELECT identity_key,at FROM listening_activity')
        membership = _PL_MEMBERSHIP
        parameters = ()
        if playlist_id is not None:
            membership += " WHERE pt.playlist_id=?"
            parameters = (playlist_id,)
        rows = self.conn.execute(
            f"WITH membership AS ({membership}), "
            "latest AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key,MAX(at) at "
            f"FROM ({activity}) GROUP BY identity_key) "
            "SELECT m.pid,MAX(a.at) last FROM membership m "
            "LEFT JOIN latest a ON a.identity_key=m.ik GROUP BY m.pid,m.ik",
            parameters).fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(r["pid"], []).append(r["last"])
        return out

    @snapshot_read
    def top_tracks(self, limit=100, since=None) -> list[dict]:
        """Songs ranked by distinct account/listening days.

        `since` (unix ts) limits the calendar-day projection for a time-windowed chart.
        """
        rows = self.conn.execute(
            "WITH plays AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key,COUNT(*) c FROM listening_days "
            "  WHERE (:since IS NULL OR day_ts>=:since) GROUP BY identity_key), "
            "     names AS (SELECT identity_key,title,artist,video_id vid,thumbnail thumb FROM playable_library_tracks) "
            "SELECT n.title, n.artist, n.vid, n.thumb, p.c FROM plays p JOIN names n ON n.identity_key=p.identity_key "
            "WHERE n.title <> '' ORDER BY p.c DESC, n.title LIMIT :limit",
            {"since": since, "limit": limit}).fetchall()
        return [{"title": r["title"], "artist": r["artist"], "video_id": r["vid"],
                 "thumbnail": r["thumb"], "plays": r["c"]} for r in rows]

    @snapshot_read
    def top_artists(self, limit=100, since=None) -> list[dict]:
        """Most-played artists from sync history: play count summed over the artist's songs."""
        rows = self.conn.execute(
            "WITH plays AS (SELECT identity_key,COUNT(*) c FROM listening_days "
            "  WHERE (:since IS NULL OR day_ts>=:since) GROUP BY identity_key), "
            "     names AS (SELECT DISTINCT t.identity_key,ta.artist_id FROM tracks t "
            f"               JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id) "
            "SELECT a.name artist,a.id artist_id,ap.provider_id browse, SUM(p.c) total, "
            f"       (SELECT MIN(t2.thumbnail) FROM tracks t2 JOIN {self.entry_artists_table} ta2 ON ta2.entry_id=t2.id "
            "        WHERE ta2.artist_id=a.id AND t2.thumbnail IS NOT NULL) thumb "
            "FROM plays p JOIN names n ON n.identity_key=p.identity_key "
            "JOIN artists a ON a.id=n.artist_id LEFT JOIN artist_provider_ids ap "
            "ON ap.artist_id=a.id AND ap.provider='youtube' "
            "WHERE a.name <> '' GROUP BY a.id ORDER BY total DESC, a.name LIMIT :limit",
            {"since": since, "limit": limit}).fetchall()
        return [{"artist": r["artist"],"artist_id":r['artist_id'],'browse':r['browse'], "plays": r["total"], "thumbnail": r["thumb"]} for r in rows]

    @synchronized
    def story_activity(self, since) -> dict:
        """Recently heard story subjects, including imported plays and collaboration credits.

        Start with the bounded listening window. Repeated uploads and library
        memberships must not multiply the evidence for a subject.
        """
        rows = self.conn.execute(
            "WITH recent AS (SELECT identity_key,COUNT(*) plays,MAX(day_ts) last_played "
            "  FROM listening_days WHERE day_ts>=? GROUP BY identity_key), "
            "credits AS ("
            "  SELECT r.*,t.artist name,t.title,t.genre FROM recent r JOIN tracks t USING(identity_key) "
            "  UNION SELECT r.*,a.name,t.title,t.genre FROM recent r JOIN tracks t USING(identity_key) "
            f"    JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id JOIN artists a ON a.id=ta.artist_id) "
            "SELECT * FROM credits ORDER BY last_played DESC,identity_key,name", (since,)).fetchall()
        result = {'artist': {}, 'genre': {}, 'recordings': {}}
        counted = set()
        for row in rows:
            for kind, name in (('artist', row['name']), ('genre', row['genre'])):
                marker = (kind, name, row['identity_key'])
                if not name or marker in counted:
                    continue
                counted.add(marker)
                evidence = result[kind].setdefault(name, {'plays': 0, 'last_played': row['last_played']})
                evidence['plays'] += row['plays']
                evidence['last_played'] = max(evidence['last_played'], row['last_played'])
            if row['name']:
                result['recordings'].setdefault(row['name'], []).append({
                    'key': row['identity_key'], 'title': row['title'], 'genre': row['genre']})
        return result

    @synchronized
    def artist_thumbnail(self, artist) -> str | None:
        """A representative thumbnail for an artist, from any of their library tracks (or None)."""
        if not artist:
            return None
        aid = artists.lookup(self.conn,artist)
        row = self.conn.execute(
            f"SELECT t.thumbnail FROM tracks t JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id "
            "WHERE ta.artist_id=? AND t.thumbnail IS NOT NULL AND t.thumbnail<>'' LIMIT 1", (aid,)).fetchone()
        return row["thumbnail"] if row else None

    @snapshot_read
    def genre_representative(self, genres) -> dict | None:
        """The most-played artist (with a thumbnail) among library tracks tagged with any of `genres`,
        as {artist, thumbnail}. Used to seed/illustrate a genre's 'into recently' card. None if empty."""
        genres = [g for g in (genres or []) if g]
        if not genres:
            return None
        qs = ",".join("?" * len(genres))
        row = self.conn.execute(
            "WITH plays AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key, "
            "COUNT(*) c FROM listening_days GROUP BY identity_key) "
            "SELECT t.artist artist, MIN(t.thumbnail) thumb, COALESCE(SUM(p.c),0) pl "
            "FROM tracks t LEFT JOIN plays p ON p.identity_key=t.identity_key "
            f"WHERE t.genre IN ({qs}) AND t.artist<>'' AND t.thumbnail IS NOT NULL AND t.thumbnail<>'' "
            "GROUP BY t.artist ORDER BY pl DESC, t.artist LIMIT 1", genres).fetchone()
        return {"artist": row["artist"], "thumbnail": row["thumb"]} if row else None

    @contextmanager
    def story_track_rows(self, *, since, artist=None, genres=()):
        """Stream ordered candidates for a controller-owned bounded read."""
        with self._lock:
            rows = self._story_track_rows(since=since, artist=artist, genres=genres)
            try:
                yield (dict(row) for row in rows) if rows is not None else iter(())
            finally:
                if rows is not None:
                    rows.close()

    @synchronized
    def _story_track_rows(self, *, since, artist, genres):
        if artist:
            aid = artists.lookup(self.conn, artist)
            if aid is None:
                return None
            selection = (f'EXISTS (SELECT 1 FROM tracks credit JOIN {self.entry_artists_table} ta ON ta.entry_id=credit.id '
                         'WHERE credit.identity_key=t.identity_key AND ta.artist_id=?)')
            params = [aid]
        elif genres:
            selection = 't.genre IN (' + ','.join('?' for _ in genres) + ')'
            params = list(genres)
        else:
            return None
        return self.conn.execute(
            'WITH plays AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key, '
            'COUNT(*) FILTER (WHERE day_ts>=?) recent_plays,MAX(day_ts) last_played '
            'FROM listening_days GROUP BY identity_key) '
            'SELECT t.identity_key key,t.title,t.artist,t.video_id,t.thumbnail,t.genre, '
            'COALESCE(hi.recent_plays,0) recent_plays,hi.last_played '
            'FROM playable_library_tracks t LEFT JOIN plays hi ON hi.identity_key=t.identity_key '
            f'WHERE {selection} AND t.title<>\'\' '
            'ORDER BY recent_plays DESC,last_played DESC,LOWER(t.title),t.identity_key',
            [since, *params])

    @snapshot_read
    def playlist_tracks_detail(self, playlist_id) -> list[dict]:
        """Full per-track detail for our own playlist view (in playlist order)."""
        return self._tracks_detail(
            "JOIN playlist_tracks pt ON pt.track_id=t.id WHERE pt.playlist_id=? ORDER BY pt.position",
            (playlist_id,))

    @snapshot_read
    def video_tracks_detail(self, video_ids) -> list[dict]:
        """Catalog detail in the requested order, independent of playlist membership."""
        if not video_ids:
            return []
        marks = ",".join("?" for _ in video_ids)
        tracks = self._tracks_detail(f"WHERE t.video_id IN ({marks})", tuple(video_ids), catalog=True)
        by_video = {t["video_id"]: t for t in tracks}
        return [by_video[video] for video in video_ids if video in by_video]

    def _tracks_detail(self, selection, params, *, catalog=False):
        rows = self.conn.execute(
            f"WITH {_PLAY_COUNTS_CTE} "
            "SELECT t.id tid, t.video_id vid, t.identity_key ikey, t.title, t.artist, t.orig_title otitle, t.orig_artist oartist, t.album, t.album_browse_id abrowse, "
            "       t.duration_s dur, t.available avail, t.thumbnail thumb, t.genre, t.mb_year, "
            f"       {_TRACK_PLAYS} plays, "
            f"      {LIKED_EXISTS} liked "
            f"FROM {'catalog_tracks' if catalog else 'tracks'} t " + selection, params).fetchall()
        return [{"track_id": r["tid"], "video_id": r["vid"], "identity_key": r["ikey"], "title": r["title"], "artist": r["artist"],
                 "title_edited": bool(r["otitle"] is not None and r["title"] != r["otitle"]),
                 "artist_edited": bool(r["oartist"] is not None and r["artist"] != r["oartist"]),
                 "album": r["album"] or "", "album_browse": r["abrowse"], "duration": r["dur"],
                 "available": r["avail"], "thumbnail": r["thumb"], "plays": r["plays"], "liked": bool(r["liked"]),
                 "genre": r["genre"] or "", "year": r["mb_year"] or ""} for r in rows]

    @snapshot_read
    def album_tracks_detail(self, album_browse_id) -> list[dict]:
        """Published release occurrences, or incidental evidence before publication.

        Keep one row per occurrence even when no catalog source is available.
        Incidental observations remain evidence without extending a complete release.
        """
        from yt_playlist.core import album_tracklists
        from yt_playlist.core.preparation import album_publication
        published, order_key = None, 'o.id'
        if album_tracklists.available(self.conn):
            published = self.conn.execute(
                "SELECT a.current_tracklist_id FROM album_provider_ids p JOIN albums a ON a.id=p.album_id "
                "WHERE p.provider='youtube' AND p.provider_id=?", (album_browse_id,)).fetchone()
            occurrence_join = "JOIN album_track_occurrences o ON o.tracklist_id=a.current_tracklist_id "
            source = ("coalesce((SELECT min(s.entry_id) FROM album_occurrence_sources s JOIN catalog_entries e ON e.id=s.entry_id "
                "WHERE s.occurrence_id=o.id AND (e.video_id=o.raw_source_ref OR e.source_ref=o.raw_source_ref)),"
                "(SELECT min(s.entry_id) FROM album_occurrence_sources s WHERE s.occurrence_id=o.id))")
        elif album_publication.selected(self.conn) is not None:
            occurrence_join = ('JOIN '+album_publication.table('groups')+' g ON g.album_id=a.id JOIN '+album_publication.table('head')+
                " h ON h.revision=g.stage AND h.active=1 AND g.status='complete' JOIN "+album_publication.table('tracks')+
                ' o ON o.stage=g.stage AND o.album_id=a.id AND o.is_full=1 ')
            published = self.conn.execute('SELECT 1 FROM album_provider_ids p JOIN albums a ON a.id=p.album_id '+occurrence_join+
                "WHERE p.provider='youtube' AND p.provider_id=? LIMIT 1", (album_browse_id,)).fetchone()
            source, order_key = 'o.entry_id', 'o.row_id'
        if published and published[0] is not None:
            rows = self.conn.execute(
                f"WITH {_PLAY_COUNTS_CTE} "
                "SELECT t.id tid,coalesce(t.video_id,'') vid, "
                "coalesce(t.title,o.captured_title,CASE WHEN o.resolution_status='unavailable' "
                "THEN 'Unavailable track' ELSE 'Unresolved track' END) title, "
                "coalesce(t.artist,o.captured_credit,a.artist,'') artist,a.title album,p.provider_id abrowse, "
                "t.duration_s dur,CASE WHEN o.resolution_status='unavailable' THEN 0 ELSE t.available END avail, "
                "coalesce(t.thumbnail,a.thumbnail) thumb,t.genre,t.mb_year, "
                f"{_TRACK_PLAYS} plays, "
                f"{LIKED_EXISTS} liked FROM album_provider_ids p JOIN albums a ON a.id=p.album_id "
                + occurrence_join + f"LEFT JOIN catalog_tracks t ON t.id={source} "
                f"WHERE p.provider='youtube' AND p.provider_id=? ORDER BY o.position IS NULL,o.position,{order_key}",
                (album_browse_id,)).fetchall()
        else:
            rows = self.conn.execute(
                f"WITH {_PLAY_COUNTS_CTE} "
                "SELECT t.id tid,t.video_id vid,t.title,t.artist,a.title album,p.provider_id abrowse, "
                "       t.duration_s dur, t.available avail, t.thumbnail thumb, t.genre, t.mb_year, "
                f"       {_TRACK_PLAYS} plays, "
                f"      {LIKED_EXISTS} liked "
                f"FROM album_provider_ids p JOIN albums a ON a.id=p.album_id JOIN {self.album_tracks_table} at ON at.album_id=a.id "
                "JOIN tracks t ON t.id=at.entry_id WHERE p.provider='youtube' AND p.provider_id=? "
                "ORDER BY at.position IS NULL,at.position,at.id", (album_browse_id,)).fetchall()
        return [{"track_id": r["tid"], "video_id": r["vid"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "album_browse": r["abrowse"], "duration": r["dur"], "available": r["avail"],
                 "thumbnail": r["thumb"], "plays": r["plays"], "liked": bool(r["liked"]),
                 "genre": r["genre"] or "", "year": r["mb_year"] or ""} for r in rows]

    @snapshot_read
    def artist_songs(self, artist, browse_id=None) -> list[dict]:
        """An artist's stored library songs: play count + which playlists hold each."""
        from yt_playlist.core import artists
        aid = artists.lookup(self.conn,artist)
        if browse_id:
            ref = self.conn.execute("SELECT artist_id FROM artist_provider_ids WHERE provider='youtube' AND provider_id=?", (browse_id,)).fetchone()
            if ref:
                aid = ref[0]
        artist_filter = f'EXISTS (SELECT 1 FROM {self.entry_artists_table} ta WHERE ta.entry_id=t.id AND ta.artist_id=?)'
        params = [aid]
        eligible = playable_tracks_sql(f'SELECT t.* FROM tracks t WHERE {artist_filter}')
        songs = self.conn.execute(
            f"WITH {_PLAY_COUNTS_CTE} "
            "SELECT t.identity_key key,t.title,t.album,t.video_id vid, "
            "       t.duration_s dur,t.thumbnail thumb,t.album_browse_id abrowse, "
            f"       {_TRACK_PLAYS} plays, "
            f"      {LIKED_EXISTS} liked "
            f"FROM ({eligible}) t", params).fetchall()
        membership = self.conn.execute(
            "SELECT DISTINCT t.identity_key key, pl.title title, pl.provider_playlist_id ytm FROM tracks t "
            "JOIN playlist_tracks pt ON pt.track_id=t.id JOIN playlists pl ON pl.id=pt.playlist_id "
            f"WHERE {artist_filter}", params).fetchall()
        by_key = {}
        for r in membership:
            by_key.setdefault(r["key"], []).append({"title": r["title"], "ytm": r["ytm"]})
        out = [{"title": r["title"], "album": r["album"] or "", "video_id": r["vid"],
                "duration": r["dur"], "plays": r["plays"], "thumbnail": r["thumb"],
                "album_browse": r["abrowse"], "liked": bool(r["liked"]),
                "playlists": sorted(by_key.get(r["key"], []), key=lambda p: p["title"].lower())}
               for r in songs]
        out.sort(key=lambda s: (-s["plays"], (s["title"] or "").lower()))
        return out
