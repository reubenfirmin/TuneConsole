"""Bounded library search facts, without sampling or display construction."""
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.core.catalog_query import playable_tracks_sql
from yt_playlist.core import album_memberships


def _like(query):
    escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


class SearchRepo(Repo):
    @synchronized
    def artist_matches(self, query, *, limit):
        pattern = _like(query)
        return [dict(row) for row in self.conn.execute(
            "WITH library_artists AS ("
            " SELECT ta.artist_id,t.identity_key,t.thumbnail FROM tracks t "
            f" JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id UNION ALL "
            " SELECT aa.artist_id,NULL,a.thumbnail FROM albums a "
            " JOIN album_artists aa ON aa.album_id=a.id "
            f" WHERE EXISTS (SELECT 1 FROM {album_memberships.read_relation(self.conn)} m WHERE m.album_id=a.id)"
            ") SELECT a.id artist_id,a.name artist,COUNT(DISTINCT l.identity_key) n,MIN(l.thumbnail) thumb "
            "FROM artists a JOIN library_artists l ON l.artist_id=a.id "
            "WHERE NOT EXISTS (SELECT 1 FROM artist_redirects r WHERE r.retired_id=a.id) "
            "AND (a.name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM artist_aliases aa "
            "WHERE aa.artist_id=a.id AND aa.alias LIKE ? ESCAPE '\\')) "
            "GROUP BY a.id ORDER BY n DESC,a.name,a.id LIMIT ?", (pattern, pattern, limit))]

    @synchronized
    def artist_tracks(self, artist_id, *, limit):
        eligible = playable_tracks_sql(
            f'SELECT t.* FROM tracks t WHERE EXISTS (SELECT 1 FROM {self.entry_artists_table} ta '
            'WHERE ta.entry_id=t.id AND ta.artist_id=?)')
        return [dict(row) for row in self.conn.execute(
            'SELECT title, artist, identity_key key, video_id vid, thumbnail thumb '
            f'FROM ({eligible}) LIMIT ?', (artist_id, limit))]

    @synchronized
    def artist_albums(self, artist_id, *, limit):
        return [dict(row) for row in self.conn.execute(
            "SELECT a.title album,p.provider_id browse,COALESCE(a.thumbnail,("
            f" SELECT MIN(t.thumbnail) FROM {self.album_tracks_table} at JOIN tracks t ON t.id=at.entry_id "
            " WHERE at.album_id=a.id)) thumb FROM albums a "
            "JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
            "WHERE NULLIF(a.title,'') IS NOT NULL AND NULLIF(p.provider_id,'') IS NOT NULL AND ("
            f" EXISTS (SELECT 1 FROM {self.album_tracks_table} at JOIN tracks t ON t.id=at.entry_id "
            f" JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id WHERE at.album_id=a.id AND ta.artist_id=?) "
            f" OR (EXISTS (SELECT 1 FROM {album_memberships.read_relation(self.conn)} m WHERE m.album_id=a.id) "
            " AND EXISTS (SELECT 1 FROM album_artists aa WHERE aa.album_id=a.id AND aa.artist_id=?))) "
            'ORDER BY a.id LIMIT ?', (artist_id, artist_id, limit))]

    @synchronized
    def artist_playlists(self, artist_id, *, excluded_group, limit):
        return [dict(row) for row in self.conn.execute(
            "SELECT p.id pid, p.title title, p.thumbnail thumb "
            "FROM playlists p JOIN playlist_tracks pt ON pt.playlist_id=p.id "
            "JOIN tracks t ON t.id=pt.track_id "
            f"JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id "
            "LEFT JOIN quarantined_playlists g ON g.playlist_scope_id=p.playlist_scope_id "
            "WHERE ta.artist_id=? AND (g.name IS NULL OR g.name<>?) "
            'GROUP BY p.id LIMIT ?', (artist_id, excluded_group, limit))]

    @synchronized
    def song_matches(self, query, *, limit):
        eligible = playable_tracks_sql("SELECT * FROM tracks WHERE title LIKE ? ESCAPE '\\'")
        return [dict(row) for row in self.conn.execute(
            'SELECT title, artist, identity_key key, video_id vid, thumbnail thumb '
            f'FROM ({eligible}) LIMIT ?', (_like(query), limit))]

    @synchronized
    def playlist_matches(self, query, *, excluded_group, limit):
        return [dict(row) for row in self.conn.execute(
            "SELECT p.id pid, p.title title, p.thumbnail thumb "
            "FROM playlists p LEFT JOIN quarantined_playlists g ON g.playlist_scope_id=p.playlist_scope_id "
            "WHERE p.title LIKE ? ESCAPE '\\' AND (g.name IS NULL OR g.name<>?) "
            'GROUP BY p.id LIMIT ?', (_like(query), excluded_group, limit))]
