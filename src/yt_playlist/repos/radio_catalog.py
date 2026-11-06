"""Bounded catalog pages and upload metadata for Radio sessions."""
from yt_playlist.repos.base import LIKED_EXISTS, Repo, lookup_read

_METADATA = ('t.identity_key AS key,t.video_id,t.title,t.artist,t.album,t.album_browse_id,t.thumbnail,'
             't.duration_s AS duration,t.genre,t.mb_year AS year,t.available,t.bpm,t.energy,t.danceability')
_RANKED = (_METADATA + f',COALESCE(p.plays,0) AS plays,{LIKED_EXISTS} AS liked,'
    'NOT EXISTS(SELECT 1 FROM catalog_entries e JOIN library_memberships m ON m.entry_id=e.id '
    'WHERE e.recording_id=t.recording_id) AS _radio_fresh')
_CATALOG = ' FROM catalog_tracks t LEFT JOIN listening_day_counts p ON p.identity_key=t.identity_key '
_PLAYABLE = " AND NULLIF(trim(t.video_id),'') IS NOT NULL AND COALESCE(t.available,1)<>0 "


class RadioCatalogRepo(Repo):
    @lookup_read
    def cluster_tracks(self, keys):
        if not keys:
            return []
        qs = ','.join('?' for _ in keys)
        return [dict(row) for row in self.conn.execute('SELECT ' + _RANKED + _CATALOG +
            f'WHERE t.identity_key IN ({qs})' + _PLAYABLE + 'ORDER BY t.id', keys)]

    @lookup_read
    def genre_labels(self):
        return [row[0] for row in self.conn.execute(
            "SELECT DISTINCT genre FROM catalog_tracks WHERE genre IS NOT NULL "
            "UNION SELECT json_extract(value_json,'$') FROM entry_field_choices WHERE field='genre'")
            if isinstance(row[0], str)]

    @lookup_read
    def genre_tracks(self, labels, after, limit):
        if not labels:
            return []
        qs = ','.join('?' for _ in labels)
        return [dict(row) for row in self.conn.execute('SELECT t.id,' + _RANKED + _CATALOG +
            f'WHERE t.id>? AND t.genre IN ({qs})' + _PLAYABLE + 'ORDER BY t.id LIMIT ?',
            (after, *labels, limit))]

    @lookup_read
    def fallback_tracks(self, limit):
        return [dict(row) for row in self.conn.execute(
            'SELECT identity_key AS key,video_id,title,artist,album,album_browse_id,thumbnail,'
            'duration_s AS duration,genre,mb_year AS year FROM playable_library_tracks '
            "WHERE video_id IS NOT NULL AND video_id<>'' LIMIT ?", (limit,))]

    @lookup_read
    def details(self, videos):
        result = {}
        videos = list(dict.fromkeys(videos))
        for offset in range(0, len(videos), 128):
            batch = videos[offset:offset + 128]
            qs = ','.join('?' for _ in batch)
            result.update({row['video_id']: dict(row) for row in self.conn.execute(
                'SELECT ' + _METADATA + f' FROM catalog_tracks t WHERE t.video_id IN ({qs})', batch)})
        return result
