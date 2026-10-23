"""SearchRepo: omnisearch over the user's library (the navbar typeahead).

LinkedIn-style: a query that matches an artist pivots into that artist's tracks /
albums / playlists; direct title matches on songs and playlists get their own
sections. Read-only and LIKE-based (no FTS). Escaping mirrors
ClusterEvidenceRepo.seed_matches, and generated playlists use the same quarantine view.

The result is a generic {query, primary_artist, sections:[{kind,title,rows}]} shape
so the Jinja partial stays dumb. Note the section key is `rows`, not `items`:
Jinja attribute lookup resolves `section.items` to the dict's .items() METHOD.
"""
import random
from urllib.parse import quote

from yt_playlist.repos.base import GENERATED_GROUP, Repo, synchronized
from yt_playlist.core.catalog_query import playable_tracks_sql

MIN_QUERY = 2                   # shorter queries are too noisy to bother
CANDIDATE_CAP = 50              # rows fetched per section before the seeded sample


def _like(q):
    """LIKE pattern that matches `q` literally; pair with ESCAPE '\\'."""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _sample(rows, q, n):
    """Up to `n` rows, randomly chosen but STABLE for a given query string (seeded by `q`)."""
    rows = list(rows)
    if len(rows) <= n:
        return rows
    return random.Random(q.lower()).sample(rows, n)


class SearchRepo(Repo):
    @synchronized
    def omni_search(self, q, *, per_section=5) -> dict:
        q = (q or "").strip()
        if len(q) < MIN_QUERY:
            return {"query": q, "primary_artist": None, "sections": []}
        like = _like(q)
        sections = []

        primary = self._artist_section(like, sections)
        shown_keys = set()
        shown_pids = set()
        if primary:
            shown_keys, shown_pids = self._artist_pivot(primary, q, per_section, sections)
        self._songs_section(like, q, per_section, shown_keys, sections)
        self._playlists_section(like, q, per_section, shown_pids, sections)
        return {"query": q, "primary_artist": primary, "sections": sections}

    # --- matched artists (ranked by track count); primary = the strongest match ---
    def _artist_section(self, like, sections):
        rows = self.conn.execute(
            "WITH library_artists AS ("
            " SELECT ta.artist_id,t.identity_key,t.thumbnail FROM tracks t "
            " JOIN track_artists ta ON ta.entry_id=t.id UNION ALL "
            " SELECT aa.artist_id,NULL,a.thumbnail FROM albums a "
            " JOIN album_artists aa ON aa.album_id=a.id "
            " WHERE EXISTS (SELECT 1 FROM album_library_memberships m WHERE m.album_id=a.id)"
            ") SELECT a.id artist_id,a.name artist,COUNT(DISTINCT l.identity_key) n,MIN(l.thumbnail) thumb "
            "FROM artists a JOIN library_artists l ON l.artist_id=a.id "
            "WHERE NOT EXISTS (SELECT 1 FROM artist_redirects r WHERE r.retired_id=a.id) "
            "AND (a.name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM artist_aliases aa "
            "WHERE aa.artist_id=a.id AND aa.alias LIKE ? ESCAPE '\\')) "
            "GROUP BY a.id ORDER BY n DESC,a.name,a.id LIMIT 3", (like,like)).fetchall()
        if not rows:
            return None
        sections.append({"kind": "artist", "title": "Artist", "rows": [
            {"type": "artist", "label": r["artist"], "artist_id": r["artist_id"],
             "sub": f"{r['n']} track" + ("" if r["n"] == 1 else "s"),
             "href": f"/artist?artist_id={r['artist_id']}",
             "thumbnail": r["thumb"], "external": False} for r in rows]})
        return {"artist_id": rows[0]["artist_id"], "name": rows[0]["artist"],
                "thumbnail": rows[0]["thumb"]}

    # --- the pivot: this artist's tracks / albums / playlists ---
    def _artist_pivot(self, primary, q, per_section, sections) -> tuple[set, set]:
        name = primary["name"]
        artist_id = primary["artist_id"]
        shown_keys = set()

        eligible = playable_tracks_sql(
            'SELECT t.* FROM tracks t WHERE EXISTS (SELECT 1 FROM track_artists ta '
            'WHERE ta.entry_id=t.id AND ta.artist_id=?)')
        trows = self.conn.execute(
            "SELECT title, artist, identity_key key, video_id vid, thumbnail thumb "
            f"FROM ({eligible}) LIMIT ?", (artist_id, CANDIDATE_CAP)).fetchall()
        picked = _sample(trows, q, per_section)
        if picked:
            shown_keys = {r["key"] for r in picked}
            sections.append({"kind": "tracks_by", "title": f"Tracks by {name}", "rows": [
                {"type": "track", "label": r["title"], "sub": r["artist"],
                 "href": "https://music.youtube.com/watch?v=" + r["vid"],
                 "thumbnail": r["thumb"], "external": True} for r in picked]})

        arows = self.conn.execute(
            "SELECT a.title album,p.provider_id browse,COALESCE(a.thumbnail,("
            " SELECT MIN(t.thumbnail) FROM album_tracks at JOIN tracks t ON t.id=at.entry_id "
            " WHERE at.album_id=a.id)) thumb FROM albums a "
            "JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
            "WHERE NULLIF(a.title,'') IS NOT NULL AND NULLIF(p.provider_id,'') IS NOT NULL AND ("
            " EXISTS (SELECT 1 FROM album_tracks at JOIN tracks t ON t.id=at.entry_id "
            " JOIN track_artists ta ON ta.entry_id=t.id WHERE at.album_id=a.id AND ta.artist_id=?) "
            " OR (EXISTS (SELECT 1 FROM album_library_memberships m WHERE m.album_id=a.id) "
            " AND EXISTS (SELECT 1 FROM album_artists aa WHERE aa.album_id=a.id AND aa.artist_id=?))) "
            "ORDER BY a.id LIMIT ?", (artist_id,artist_id,CANDIDATE_CAP)).fetchall()
        picked = _sample(arows, q, per_section)
        if picked:
            sections.append({"kind": "albums_by", "title": f"Albums by {name}", "rows": [
                {"type": "album", "label": r["album"], "sub": name,
                 "href": "/album?browse=" + quote(r["browse"]),
                 "thumbnail": r["thumb"], "external": False} for r in picked]})

        prows = self.conn.execute(
            "SELECT p.id pid, p.title title, p.thumbnail thumb "
            "FROM playlists p JOIN playlist_tracks pt ON pt.playlist_id=p.id "
            "JOIN tracks t ON t.id=pt.track_id "
            "JOIN track_artists ta ON ta.entry_id=t.id "
            "LEFT JOIN quarantined_playlists g ON g.ytm=p.scope_ref "
            "WHERE ta.artist_id=? AND (g.name IS NULL OR g.name<>?) "
            "GROUP BY p.id LIMIT ?", (artist_id, GENERATED_GROUP, CANDIDATE_CAP)).fetchall()
        picked = _sample(prows, q, per_section)
        shown_pids = set()
        if picked:
            shown_pids = {r["pid"] for r in picked}
            sections.append({"kind": "playlists_featuring",
                             "title": f"Playlists featuring {name}", "rows": [
                {"type": "playlist", "label": r["title"], "sub": "playlist",
                 "href": f"/playlist/{r['pid']}", "thumbnail": r["thumb"],
                 "external": False} for r in picked]})
        return shown_keys, shown_pids

    # --- direct song-title matches, minus anything already under Tracks by <artist> ---
    def _songs_section(self, like, q, per_section, shown_keys, sections):
        eligible = playable_tracks_sql("SELECT * FROM tracks WHERE title LIKE ? ESCAPE '\\'")
        rows = self.conn.execute(
            "SELECT title, artist, identity_key key, video_id vid, thumbnail thumb "
            f"FROM ({eligible}) LIMIT ?", (like, CANDIDATE_CAP)).fetchall()
        rows = [r for r in rows if r["key"] not in shown_keys]
        picked = _sample(rows, q, per_section)
        if picked:
            sections.append({"kind": "songs", "title": "Songs", "rows": [
                {"type": "track", "label": r["title"], "sub": r["artist"],
                 "href": "https://music.youtube.com/watch?v=" + r["vid"],
                 "thumbnail": r["thumb"], "external": True} for r in picked]})

    # --- direct playlist-title matches, minus generated + ones already shown ---
    def _playlists_section(self, like, q, per_section, shown_pids, sections):
        rows = self.conn.execute(
            "SELECT p.id pid, p.title title, p.thumbnail thumb "
            "FROM playlists p LEFT JOIN quarantined_playlists g ON g.ytm=p.scope_ref "
            "WHERE p.title LIKE ? ESCAPE '\\' AND (g.name IS NULL OR g.name<>?) "
            "GROUP BY p.id LIMIT ?", (like, GENERATED_GROUP, CANDIDATE_CAP)).fetchall()
        rows = [r for r in rows if r["pid"] not in shown_pids]
        picked = _sample(rows, q, per_section)
        if picked:
            sections.append({"kind": "playlists", "title": "Playlists", "rows": [
                {"type": "playlist", "label": r["title"], "sub": "playlist",
                 "href": f"/playlist/{r['pid']}", "thumbnail": r["thumb"],
                 "external": False} for r in picked]})
