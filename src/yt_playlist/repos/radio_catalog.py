"""Bounded catalog pages and upload metadata for Radio sessions."""
from yt_playlist.library.rating_policy import LIKED_EXISTS
from yt_playlist.repos.base import Repo, lookup_read

_METADATA = ('t.identity_key AS key,t.video_id,t.title,t.artist,t.album,t.album_browse_id,t.thumbnail,'
             't.duration_s AS duration,t.genre,t.mb_year AS year,t.available,t.bpm,t.energy,t.danceability')
_RANKED = (_METADATA + f',COALESCE(p.plays,0) AS plays,{LIKED_EXISTS} AS liked,'
    'NOT EXISTS(SELECT 1 FROM catalog_entries e JOIN library_memberships m ON m.entry_id=e.id '
    'WHERE e.recording_id=t.recording_id) AS _radio_fresh')
_CATALOG = ' FROM catalog_tracks t LEFT JOIN listening_day_counts p ON p.identity_key=t.identity_key '
_PLAYABLE = " AND NULLIF(trim(t.video_id),'') IS NOT NULL AND COALESCE(t.available,1)<>0 "


class RadioCatalogRepo(Repo):
    @lookup_read
    def rotation_facts(self, keys):
        """Current ownership and any listening, including generated Radio starts.

        Probe only requested identities through recording/time and key/time
        indexes. The taste-learning day-count view deliberately excludes some
        listening and cannot establish that a recording has never been heard.
        """
        from yt_playlist.core import listening_subject_reads, served_facts
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        native = listening_subject_reads.native(self.conn)
        subject = 'unresolved_subject_key' if native else 'identity_key'
        live = served_facts.expression(self.conn, 'policy_kind',
            "COALESCE(json_extract(i.provenance,'$.policy_kind'),'live')", alias='i')
        result = {}
        for offset in range(0, len(keys), 128):
            batch = keys[offset:offset + 128]
            marks = ','.join('?' for _ in batch)
            rows = self.conn.execute(
                'SELECT r.identity_key AS key,'
                'EXISTS(SELECT 1 FROM catalog_entries e JOIN library_memberships m ON m.entry_id=e.id '
                'WHERE e.recording_id=r.id) AS owned,'
                '(EXISTS(SELECT 1 FROM play_events p WHERE p.recording_id=r.id) OR '
                'EXISTS(SELECT 1 FROM listening_observations o WHERE o.recording_id=r.id) OR '
                f'EXISTS(SELECT 1 FROM play_events p WHERE p.recording_id IS NULL AND p.{subject}=r.identity_key) OR '
                f'EXISTS(SELECT 1 FROM listening_observations o WHERE o.recording_id IS NULL AND o.{subject}=r.identity_key) OR '
                "EXISTS(SELECT 1 FROM rec_response_events x WHERE x.identity_key=r.identity_key AND x.kind IN ('start','play'))) AS heard,"
                '(SELECT max(p.played_at) FROM play_events p WHERE p.recording_id=r.id) AS last_played,'
                "(SELECT max(x.at) FROM rec_response_events x WHERE x.identity_key=r.identity_key AND x.kind IN ('start','play')) AS last_response,"
                '(SELECT max(i.served_at) FROM rec_served_impressions i WHERE i.identity_key=r.identity_key '
                f"AND i.surface IN ('radio_seed','radio_directions') AND {live}='live') AS last_offered "
                f'FROM recordings r WHERE r.identity_key IN ({marks})', batch)
            for row in rows:
                fact = dict(row)
                times = (fact.pop('last_played'), fact.pop('last_response'))
                fact['last_started'] = max((at for at in times if at is not None), default=None)
                result[row['key']] = fact
        return result

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
    def fallback_tracks(self, limit, *, after=0):
        return [dict(row) for row in self.conn.execute(
            'SELECT id,identity_key AS key,video_id,title,artist,album,album_browse_id,thumbnail,'
            'duration_s AS duration,genre,mb_year AS year FROM playable_library_tracks '
            "WHERE id>? AND video_id IS NOT NULL AND video_id<>'' ORDER BY id LIMIT ?", (after, limit))]

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
