"""ChartsRepo: play-history statistics for the charts / artist / playlist UI pages
(most-played songs and artists, per-playlist listen stats, and per-track detail views).
"""
from yt_playlist.repos.base import LIKED_EXISTS, Repo, synchronized
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
                     "FROM tracks t JOIN album_tracks at ON at.entry_id=t.id")


class ChartsRepo(Repo):
    @synchronized
    def ticker_thumbnails(self, dimension) -> dict:
        """Local artwork for chart categories, in one query per dimension."""
        if dimension == "artist":
            rows = self.conn.execute(
                "SELECT artist cat, MIN(NULLIF(thumbnail,'')) thumbnail FROM tracks "
                "WHERE artist<>'' GROUP BY artist").fetchall()
        elif dimension == "album":
            rows = self.conn.execute(
                "SELECT '@album:'||a.id cat,COALESCE(NULLIF(a.thumbnail,''),MIN(NULLIF(t.thumbnail,''))) thumbnail "
                "FROM albums a JOIN album_tracks at ON at.album_id=a.id JOIN tracks t ON t.id=at.entry_id "
                "GROUP BY a.id").fetchall()
        elif dimension == "playlist":
            rows = self.conn.execute(
                "SELECT p.title cat, COALESCE(MIN(NULLIF(p.thumbnail,'')), "
                "MIN(NULLIF(t.thumbnail,''))) thumbnail FROM playlists p "
                "LEFT JOIN playlist_tracks pt ON pt.playlist_id=p.id "
                "LEFT JOIN tracks t ON t.id=pt.track_id GROUP BY p.title").fetchall()
        else:
            raise ValueError(f"No ticker artwork for dimension: {dimension}")
        return {r["cat"]: r["thumbnail"] for r in rows if r["thumbnail"]}

    @synchronized
    def album_browse_ids(self) -> dict:
        """Chart release keys to their exact YouTube browse IDs."""
        rows = self.conn.execute(
            "SELECT '@album:'||a.id k,p.provider_id b FROM albums a JOIN album_provider_ids p "
            "ON p.album_id=a.id AND p.provider='youtube' WHERE EXISTS "
            "(SELECT 1 FROM album_tracks at JOIN tracks t ON t.id=at.entry_id WHERE at.album_id=a.id)").fetchall()
        return {r['k']:r['b'] for r in rows}

    @synchronized
    def album_labels(self):
        return {f'@album:{r[0]}':r[1] or 'Album' for r in self.conn.execute('SELECT id,title FROM albums')}

    @synchronized
    def history_bounds(self) -> tuple:
        """(earliest, latest) snapshot taken_at across all sync history, or (None, None) if empty.
        Lets the ticker size its candle periods to how much history actually exists."""
        r = self.conn.execute(
            "SELECT MIN(day_ts) lo,MAX(day_ts) hi FROM listening_days").fetchone()
        if r is None or r["lo"] is None:
            return (None, None)
        return (r["lo"], r["hi"])

    @synchronized
    def corpus_distribution(self, dimension) -> dict:
        """Library composition for a ticker dimension: {category: song_count}, counted per
        distinct identity_key (one song = one unit) so it's comparable with listen_distribution.

        dimension: 'genre' | 'year' | 'album' | 'playlist'. Untagged songs are excluded.
        Playlist counts a song once per playlist it belongs to (memberships sum > #songs).
        """
        if dimension == "playlist":
            rows = self.conn.execute(
                f"WITH pl AS ({_PL_MEMBERSHIP}) "
                "SELECT p.title cat, COUNT(DISTINCT pl.ik) c "
                "FROM pl JOIN playlists p ON p.id=pl.pid GROUP BY p.id").fetchall()
        elif dimension == 'album':
            rows = self.conn.execute(
                f"WITH membership AS ({_ALBUM_MEMBERSHIP}) "
                "SELECT '@album:'||aid cat,COUNT(*) c FROM membership GROUP BY aid").fetchall()
        else:
            rows = self.conn.execute(
                f"WITH ident AS (SELECT identity_key, {_CAT_EXPR[dimension]} cat "
                "FROM tracks GROUP BY identity_key) "
                "SELECT cat, COUNT(*) c FROM ident WHERE cat IS NOT NULL GROUP BY cat").fetchall()
        return {r["cat"]: r["c"] for r in rows}

    @synchronized
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
                "SELECT p.title cat, COUNT(*) c "
                "FROM listening_days hi "
                "JOIN pl ON pl.ik=hi.identity_key JOIN playlists p ON p.id=pl.pid "
                f"WHERE {bounds} GROUP BY p.id", args).fetchall()
        elif dimension == 'album':
            rows = self.conn.execute(
                f"WITH membership AS ({_ALBUM_MEMBERSHIP}) "
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

    @synchronized
    def get_playlist_listen_stats(self) -> dict:
        """Activity per playlist: exact repeats plus unmatched coarse days.

        Recording membership is deduplicated before joining evidence, so another
        confirmed upload changes playback membership without multiplying taste weight.
        """
        rows = self.conn.execute(
            f"WITH membership AS ({_PL_MEMBERSHIP}) "
            "SELECT m.pid,COUNT(*) cnt,MAX(a.at) last FROM membership m "
            "JOIN listening_activity a ON a.identity_key=m.ik GROUP BY m.pid").fetchall()
        return {r["pid"]: (r["last"], r["cnt"]) for r in rows}

    @synchronized
    def get_playlist_track_recency(self) -> dict:
        """Per-playlist {playlist_id: [per-track last-played ts | None, ...]}, one entry per distinct
        track, its newest snapshot (None = never played). Unlike get_playlist_listen_stats (which
        collapses to the single freshest track), this keeps every track's recency so callers can judge
        a playlist by the *aggregate* staleness of its tracks, not its one most-recently-played song.
        """
        rows = self.conn.execute(
            f"WITH membership AS ({_PL_MEMBERSHIP}) "
            "SELECT m.pid,MAX(a.at) last FROM membership m "
            "LEFT JOIN listening_activity a ON a.identity_key=m.ik GROUP BY m.pid,m.ik").fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(r["pid"], []).append(r["last"])
        return out

    @synchronized
    def top_tracks(self, limit=100, since=None) -> list[dict]:
        """Songs ranked by distinct account/listening days.

        `since` (unix ts) limits the calendar-day projection for a time-windowed chart.
        """
        rows = self.conn.execute(
            "WITH plays AS (SELECT identity_key,COUNT(*) c FROM listening_days "
            "  WHERE (:since IS NULL OR day_ts>=:since) GROUP BY identity_key), "
            "     names AS (SELECT identity_key,title,artist,video_id vid,thumbnail thumb FROM playable_library_tracks) "
            "SELECT n.title, n.artist, n.vid, n.thumb, p.c FROM plays p JOIN names n ON n.identity_key=p.identity_key "
            "WHERE n.title <> '' ORDER BY p.c DESC, n.title LIMIT :limit",
            {"since": since, "limit": limit}).fetchall()
        return [{"title": r["title"], "artist": r["artist"], "video_id": r["vid"],
                 "thumbnail": r["thumb"], "plays": r["c"]} for r in rows]

    @synchronized
    def top_artists(self, limit=100, since=None) -> list[dict]:
        """Most-played artists from sync history: play count summed over the artist's songs."""
        rows = self.conn.execute(
            "WITH plays AS (SELECT identity_key,COUNT(*) c FROM listening_days "
            "  WHERE (:since IS NULL OR day_ts>=:since) GROUP BY identity_key), "
            "     names AS (SELECT DISTINCT t.identity_key,ta.artist_id FROM tracks t "
            "               JOIN track_artists ta ON ta.entry_id=t.id) "
            "SELECT a.name artist,a.id artist_id,ap.provider_id browse, SUM(p.c) total, "
            "       (SELECT MIN(t2.thumbnail) FROM tracks t2 JOIN track_artists ta2 ON ta2.entry_id=t2.id "
            "        WHERE ta2.artist_id=a.id AND t2.thumbnail IS NOT NULL) thumb "
            "FROM plays p JOIN names n ON n.identity_key=p.identity_key "
            "JOIN artists a ON a.id=n.artist_id LEFT JOIN artist_provider_ids ap "
            "ON ap.artist_id=a.id AND ap.provider='youtube' "
            "WHERE a.name <> '' GROUP BY a.id ORDER BY total DESC, a.name LIMIT :limit",
            {"since": since, "limit": limit}).fetchall()
        return [{"artist": r["artist"],"artist_id":r['artist_id'],'browse':r['browse'], "plays": r["total"], "thumbnail": r["thumb"]} for r in rows]

    @synchronized
    def artist_thumbnail(self, artist) -> str | None:
        """A representative thumbnail for an artist, from any of their library tracks (or None)."""
        if not artist:
            return None
        aid = artists.lookup(self.conn,artist)
        row = self.conn.execute(
            "SELECT t.thumbnail FROM tracks t JOIN track_artists ta ON ta.entry_id=t.id "
            "WHERE ta.artist_id=? AND t.thumbnail IS NOT NULL AND t.thumbnail<>'' LIMIT 1", (aid,)).fetchone()
        return row["thumbnail"] if row else None

    @synchronized
    def genre_representative(self, genres) -> dict | None:
        """The most-played artist (with a thumbnail) among library tracks tagged with any of `genres`,
        as {artist, thumbnail}. Used to seed/illustrate a genre's 'into recently' card. None if empty."""
        genres = [g for g in (genres or []) if g]
        if not genres:
            return None
        qs = ",".join("?" * len(genres))
        row = self.conn.execute(
            "WITH plays AS (SELECT identity_key, COUNT(*) c FROM listening_days GROUP BY identity_key) "
            "SELECT t.artist artist, MIN(t.thumbnail) thumb, COALESCE(SUM(p.c),0) pl "
            "FROM tracks t LEFT JOIN plays p ON p.identity_key=t.identity_key "
            f"WHERE t.genre IN ({qs}) AND t.artist<>'' AND t.thumbnail IS NOT NULL AND t.thumbnail<>'' "
            "GROUP BY t.artist ORDER BY pl DESC, t.artist LIMIT 1", genres).fetchone()
        return {"artist": row["artist"], "thumbnail": row["thumb"]} if row else None

    @synchronized
    def playlist_tracks_detail(self, playlist_id) -> list[dict]:
        """Full per-track detail for our own playlist view (in playlist order)."""
        rows = self.conn.execute(
            "SELECT t.id tid, t.video_id vid, t.identity_key ikey, t.title, t.artist, t.orig_title otitle, t.orig_artist oartist, t.album, t.album_browse_id abrowse, "
            "       t.duration_s dur, t.available avail, t.thumbnail thumb, t.genre, t.mb_year, "
            "       (SELECT COUNT(*) FROM listening_days hi WHERE hi.identity_key=t.identity_key) plays, "
            f"      {LIKED_EXISTS} liked "
            "FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? ORDER BY pt.position", (playlist_id,)).fetchall()
        return [{"track_id": r["tid"], "video_id": r["vid"], "identity_key": r["ikey"], "title": r["title"], "artist": r["artist"],
                 "title_edited": bool(r["otitle"] is not None and r["title"] != r["otitle"]),
                 "artist_edited": bool(r["oartist"] is not None and r["artist"] != r["oartist"]),
                 "album": r["album"] or "", "album_browse": r["abrowse"], "duration": r["dur"],
                 "available": r["avail"], "thumbnail": r["thumb"], "plays": r["plays"], "liked": bool(r["liked"]),
                 "genre": r["genre"] or "", "year": r["mb_year"] or ""} for r in rows]

    @synchronized
    def album_tracks_detail(self, album_browse_id) -> list[dict]:
        """Per-track detail for a saved album's folded-in tracks (same shape as playlist_tracks_detail,
        so the row partial renders identically). Retain full-fetch order when known."""
        rows = self.conn.execute(
            "SELECT t.id tid,t.video_id vid,t.title,t.artist,a.title album,p.provider_id abrowse, "
            "       t.duration_s dur, t.available avail, t.thumbnail thumb, t.genre, t.mb_year, "
            "       (SELECT COUNT(*) FROM listening_days hi WHERE hi.identity_key=t.identity_key) plays, "
            f"      {LIKED_EXISTS} liked "
            "FROM album_provider_ids p JOIN albums a ON a.id=p.album_id JOIN album_tracks at ON at.album_id=a.id "
            "JOIN tracks t ON t.id=at.entry_id WHERE p.provider='youtube' AND p.provider_id=? "
            "ORDER BY at.position IS NULL,at.position,at.id", (album_browse_id,)).fetchall()
        return [{"track_id": r["tid"], "video_id": r["vid"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "album_browse": r["abrowse"], "duration": r["dur"], "available": r["avail"],
                 "thumbnail": r["thumb"], "plays": r["plays"], "liked": bool(r["liked"]),
                 "genre": r["genre"] or "", "year": r["mb_year"] or ""} for r in rows]

    @synchronized
    def artist_songs(self, artist, browse_id=None) -> list[dict]:
        """An artist's stored library songs: play count + which playlists hold each."""
        from yt_playlist.core import artists
        aid = artists.lookup(self.conn,artist)
        if browse_id:
            ref = self.conn.execute("SELECT artist_id FROM artist_provider_ids WHERE provider='youtube' AND provider_id=?", (browse_id,)).fetchone()
            aid = ref[0] if ref else None
        artist_filter = 'EXISTS (SELECT 1 FROM track_artists ta WHERE ta.entry_id=t.id AND ta.artist_id=?)'
        params = [aid]
        eligible = playable_tracks_sql(f'SELECT t.* FROM tracks t WHERE {artist_filter}')
        songs = self.conn.execute(
            "SELECT t.identity_key key,t.title,t.album,t.video_id vid, "
            "       t.duration_s dur,t.thumbnail thumb,t.album_browse_id abrowse, "
            "       (SELECT COUNT(*) FROM listening_days hi WHERE hi.identity_key=t.identity_key) plays, "
            f"      {LIKED_EXISTS} liked "
            f"FROM ({eligible}) t", params).fetchall()
        membership = self.conn.execute(
            "SELECT DISTINCT t.identity_key key, pl.title title, pl.ytm_playlist_id ytm FROM tracks t "
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
