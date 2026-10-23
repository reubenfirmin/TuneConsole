"""Detached library evidence for seed lookup and track relationships.

These reads know about catalogue joins and evidence eligibility. They do not
choose explanation kinds, rank explanations, or construct surface responses.
"""
from dataclasses import dataclass

from yt_playlist.repos.base import GENERATED_GROUP, Repo, synchronized
from yt_playlist.repos.playlist_eligibility import QUARANTINED_PLAYLIST_IDS_SQL
from yt_playlist.util.matching import search_squash


@dataclass(frozen=True)
class PlaylistNeighbor:
    playlist_id: int
    artist: str


@dataclass(frozen=True)
class AlbumNeighbor:
    title: str
    artist: str


@dataclass(frozen=True)
class ConnectionEvidence:
    artist: str = ""
    genre: str = ""
    year: str = ""
    same_artist: str | None = None
    playlists: tuple[PlaylistNeighbor, ...] = ()
    album: AlbumNeighbor | None = None
    same_day: bool = False
    path_genres: tuple[str, ...] = ()
    path_years: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeedGroup:
    name: str
    count: int
    keys: tuple[str, ...]


@dataclass(frozen=True)
class SeedSong:
    title: str
    artist: str
    key: str


@dataclass(frozen=True)
class SeedMatches:
    artists: tuple[SeedGroup, ...] = ()
    playlists: tuple[SeedGroup, ...] = ()
    songs: tuple[SeedSong, ...] = ()


class ClusterEvidenceRepo(Repo):
    @synchronized
    def connection(self, key, path_keys, *, max_playlist=120) -> ConnectionEvidence:
        """Relationships to distinct other tracks, without interpreting the facts.

        Catch-all and quarantined playlists cannot support a shared-playlist
        relationship. Listening-day evidence proves a shared account/day only.
        All fields are detached from SQLite before the lock is released.
        """
        path = [k for k in dict.fromkeys(path_keys) if k and k != key]
        if not key or not path:
            return ConnectionEvidence()
        qs = ",".join("?" * len(path))
        child = self.conn.execute(
            "SELECT MIN(artist) artist, MIN(NULLIF(genre,'')) genre, "
            "MIN(NULLIF(mb_year,'')) yr FROM tracks WHERE identity_key=?", (key,)).fetchone()
        same_artist = self.conn.execute(
            "SELECT t2.artist a FROM tracks t1 JOIN tracks t2 "
            "ON LOWER(TRIM(t2.artist))=LOWER(TRIM(t1.artist)) "
            f"WHERE t1.identity_key=? AND TRIM(t1.artist)<>'' AND t2.identity_key IN ({qs}) LIMIT 1",
            [key, *path]).fetchone()
        playlists = self.conn.execute(
            "SELECT cp.pid pid,t.artist artist FROM "
            "(SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            " JOIN tracks t ON t.id=pt.track_id WHERE t.identity_key=?) cp "
            "JOIN playlist_tracks pt ON pt.playlist_id=cp.pid JOIN tracks t ON t.id=pt.track_id "
            f"WHERE t.identity_key IN ({qs}) "
            "AND cp.pid NOT IN (SELECT playlist_id FROM playlist_tracks "
            "                   GROUP BY playlist_id HAVING COUNT(*)>?) "
            f"AND cp.pid NOT IN ({QUARANTINED_PLAYLIST_IDS_SQL})",
            [key, *path, max_playlist, GENERATED_GROUP]).fetchall()
        album = self.conn.execute(
            "SELECT a.title al,t2.artist ar FROM tracks t1 JOIN album_tracks a1 ON a1.entry_id=t1.id "
            "JOIN album_tracks a2 ON a2.album_id=a1.album_id JOIN albums a ON a.id=a1.album_id "
            "JOIN tracks t2 ON t2.id=a2.entry_id "
            f"WHERE t1.identity_key=? AND NULLIF(a.title,'') IS NOT NULL AND t2.identity_key IN ({qs}) "
            "AND t2.identity_key<>? LIMIT 1", [key, *path, key]).fetchone()
        same_day = self.conn.execute(
            "SELECT 1 FROM listening_days h1 JOIN listening_days h2 "
            "ON h2.identity_id=h1.identity_id AND h2.day=h1.day "
            f"WHERE h1.identity_key=? AND h2.identity_key IN ({qs}) LIMIT 1", [key, *path]).fetchone()
        genres = self.conn.execute(
            f"SELECT DISTINCT genre FROM tracks WHERE identity_key IN ({qs}) AND genre<>''", path).fetchall()
        years = self.conn.execute(
            f"SELECT DISTINCT mb_year FROM tracks WHERE identity_key IN ({qs}) AND mb_year<>''", path).fetchall()
        return ConnectionEvidence(
            artist=(child['artist'] or '').strip(), genre=child['genre'] or '', year=child['yr'] or '',
            same_artist=same_artist['a'] if same_artist else None,
            playlists=tuple(PlaylistNeighbor(r['pid'], r['artist'] or '') for r in playlists),
            album=AlbumNeighbor(album['al'], album['ar']) if album else None,
            same_day=same_day is not None,
            path_genres=tuple(r['genre'] for r in genres), path_years=tuple(r['mb_year'] for r in years))

    @synchronized
    def seed_matches(self, query, *, limit=8) -> SeedMatches:
        """Vector-backed groups and songs, with database relevance order intact.

        Normalization is shared with searchnorm(): punctuation, whitespace, and
        accents do not affect matches. The limit applies independently per kind.
        """
        qn = search_squash(query or '')
        if not qn:
            return SeedMatches()
        like, prefix = '%' + qn + '%', qn + '%'
        artists = self.conn.execute(
            "SELECT t.artist label,COUNT(DISTINCT t.identity_key) n,"
            "GROUP_CONCAT(DISTINCT t.identity_key) keys "
            "FROM tracks t JOIN rec_vectors rv ON rv.identity_key=t.identity_key "
            "WHERE searchnorm(t.artist) LIKE ? AND t.artist<>'' "
            "GROUP BY t.artist ORDER BY n DESC LIMIT ?", (like, limit)).fetchall()
        playlists = self.conn.execute(
            "SELECT p.title label,COUNT(DISTINCT t.identity_key) n,"
            "GROUP_CONCAT(DISTINCT t.identity_key) keys "
            "FROM playlists p JOIN playlist_tracks pt ON pt.playlist_id=p.id "
            "JOIN tracks t ON t.id=pt.track_id JOIN rec_vectors rv ON rv.identity_key=t.identity_key "
            f"WHERE searchnorm(p.title) LIKE ? AND p.id NOT IN ({QUARANTINED_PLAYLIST_IDS_SQL}) "
            "GROUP BY p.id ORDER BY n DESC LIMIT ?", (like, GENERATED_GROUP, limit)).fetchall()
        songs = self.conn.execute(
            "SELECT MIN(t.title) title,MIN(t.artist) artist,t.identity_key key "
            "FROM tracks t JOIN rec_vectors rv ON rv.identity_key=t.identity_key "
            "WHERE searchnorm(t.title) LIKE ? GROUP BY t.identity_key "
            "ORDER BY CASE WHEN searchnorm(MIN(t.title))=? THEN 0 "
            "WHEN searchnorm(MIN(t.title)) LIKE ? THEN 1 ELSE 2 END,"
            "length(MIN(t.title)),MIN(t.title) LIMIT ?", (like, qn, prefix, limit)).fetchall()
        return SeedMatches(
            artists=tuple(SeedGroup(r['label'], r['n'], tuple(r['keys'].split(','))) for r in artists),
            playlists=tuple(SeedGroup(r['label'], r['n'], tuple(r['keys'].split(','))) for r in playlists),
            songs=tuple(SeedSong(r['title'], r['artist'], r['key']) for r in songs))
