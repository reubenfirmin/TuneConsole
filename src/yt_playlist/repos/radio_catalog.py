"""Bounded catalog pages and upload metadata for Radio sessions."""
from yt_playlist.core import served_facts
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.core.catalog_query import (
    is_playable_sql, library_recordings_sql, playable_tracks_sql, PLAYABLE_LIBRARY_SQL,
)
from yt_playlist.library.rating_policy import LIKED_EXISTS
from yt_playlist.repos.base import Repo, lookup_read

_METADATA = ('t.identity_key AS key,t.video_id,t.title,t.artist,t.album,t.album_browse_id,t.thumbnail,'
             't.duration_s AS duration,t.genre,t.mb_year AS year,t.available,t.bpm,t.danceability')
_RANKED = (_METADATA + f',COALESCE(p.plays,0) AS plays,{LIKED_EXISTS} AS liked,'
    f'NOT EXISTS({library_recordings_sql("SELECT id,identity_key FROM recordings WHERE id=t.recording_id")}) '
    'AS _radio_fresh')
_CATALOG = ' FROM catalog_tracks t LEFT JOIN listening_day_counts p ON p.identity_key=t.identity_key '
_PLAYABLE = f" AND {is_playable_sql('t')} "
_OWNED = '(r.has_saved_source OR r.has_eligible_listen OR r.in_collection_album)'
_LISTENING_CANDIDATES = '''
    listening AS (
        SELECT q.id,e.played_at AS started FROM subjects q CROSS JOIN play_events e ON e.recording_id=q.id
        UNION ALL SELECT q.id,NULL FROM subjects q CROSS JOIN listening_observations e ON e.recording_id=q.id
        UNION ALL SELECT q.id,NULL FROM subjects q CROSS JOIN play_events e
              ON e.recording_id IS NULL AND e.unresolved_subject_key=q.identity_key
        UNION ALL SELECT q.id,NULL FROM subjects q CROSS JOIN listening_observations e
              ON e.recording_id IS NULL AND e.unresolved_subject_key=q.identity_key
        UNION ALL SELECT q.id,e.at FROM subjects q CROSS JOIN rec_response_events e
              ON e.identity_key=q.identity_key AND e.kind IN ('start','play')
    ), heard AS MATERIALIZED (
        SELECT id,MAX(started) AS last_started FROM listening GROUP BY id
    )
'''
_LIVE_IMPRESSION = served_facts.expression(
    'policy_kind', "COALESCE(json_extract(i.provenance,'$.policy_kind'),'live')", alias='i')
# Look up each subject by identity. SQLite can otherwise choose the live-policy
# index and scan its live entries again for every subject.
_OFFERED_CANDIDATES = f'''
    offered AS (
        SELECT q.id,MAX(i.served_at) AS last_offered FROM subjects q
        CROSS JOIN rec_served_impressions i INDEXED BY ix_rec_served_key_at
          ON i.identity_key=q.identity_key
        WHERE i.surface IN ('radio_seed','radio_directions')
          AND {_LIVE_IMPRESSION}='live'
        GROUP BY q.id
    )
'''


class RadioCatalogRepo(Repo):
    @lookup_read
    def tuning_ranks(self, generation, surface, purpose, *, intensity=None, fresh_surface=None, pinned_genre=None,
                     maximum_duration=None):
        """Read eligible recording ranks before loading any musical or display profiles."""
        fresh = purpose == 'fresh'
        playable = "NULLIF(trim(s.video_id),'') IS NOT NULL AND s.effective_available IS NOT 0"
        params = dict(generation=generation, surface=surface, purpose=purpose)
        if maximum_duration is not None:
            params['maximum_duration'] = maximum_duration
            playable += ' AND (s.effective_duration_s IS NULL OR s.effective_duration_s<=:maximum_duration)'
        owned_uploads = '' if fresh else f'''
            UNION ALL
            SELECT q.id,COALESCE(MIN(s.id) FILTER (WHERE s.library_origin IS NOT NULL),MIN(s.id))
            FROM subjects q CROSS JOIN track_sources s ON s.recording_id=q.id
            WHERE q.owned AND {playable} GROUP BY q.id
        '''
        eligibility = (f"r.is_discovery AND NOT {_OWNED} AND k.mode_key='all'" if fresh
                       else f'({_OWNED} OR r.is_discovery)')
        catalog_ranks = ''
        if pinned_genre:
            params['pinned_genre'] = pinned_genre
            catalog_ranks = f'''
                UNION ALL
                SELECT '', 'catalog', NULL, r.id, MIN(s.id), NULL, NULL
                FROM recordings r
                LEFT JOIN genre_catalog gd ON gd.key=r.effective_genre_key
                CROSS JOIN track_sources s ON s.recording_id=r.id
                WHERE COALESCE(gd.redirect_key,r.effective_genre_key)=:pinned_genre AND {playable}
                GROUP BY r.id
            '''
            eligibility = f"({eligibility} OR k.purpose='catalog')"
        fit_cte, fit_join, fit_score = '', '', 'NULL'
        if intensity is not None:
            profile_version, measurement, joint, sources = intensity
            params.update(profile_version=profile_version, measurement=measurement, joint=joint,
                          **{f'source{i}': source for i, source in enumerate(sources)})
            source_params = ','.join(f':source{i}' for i in range(len(sources)))
            channels = ','.join(f"'{channel}'" for channel in INTENSITY_CHANNELS)
            fit_cte = f''', intensity AS (
                SELECT q.id,MAX(v.score) AS score FROM subjects q
                CROSS JOIN recordings m ON m.id=q.id AND m.musical_profile_version=:profile_version
                CROSS JOIN recording_vibes v ON v.recording_id=q.id AND v.vibe IN ({channels})
                JOIN vibe_versions vs ON vs.id=v.inputs_version_id
                WHERE v.support>0 AND v.source IN ({source_params})
                  AND ((v.source='audio_measurement' AND vs.value GLOB :measurement)
                       OR (v.prediction_id IS NOT NULL AND vs.value GLOB :joint))
                GROUP BY q.id
            )'''
            fit_join, fit_score = 'LEFT JOIN intensity i ON i.id=q.id', 'i.score'
        pool_scope = 'p.surface=:surface AND p.purpose=:purpose'
        if fresh_surface is not None:
            params['fresh_surface'] = fresh_surface
            pool_scope = f"({pool_scope} OR (p.surface=:fresh_surface AND p.purpose='fresh' AND p.mode_key='all'))"
        needs_heard = fresh or fresh_surface is not None
        heard_cte = ', ' + _LISTENING_CANDIDATES if needs_heard else ''
        heard_join = 'LEFT JOIN heard h ON h.id=q.id' if needs_heard else ''
        heard = 'h.id IS NOT NULL' if needs_heard else 'NULL'
        last_started = 'h.last_started' if needs_heard else 'NULL'
        offered_cte = ', ' + _OFFERED_CANDIDATES if fresh_surface is not None else ''
        offered_join = 'LEFT JOIN offered o ON o.id=q.id' if offered_cte else ''
        last_offered = 'o.last_offered' if offered_cte else 'NULL'
        fresh_only = f"AND (k.purpose<>'fresh' OR (NOT q.owned AND NOT ({heard})))" if needs_heard else ''
        rows = self.conn.execute(f'''
            WITH ranks AS MATERIALIZED (
                SELECT p.mode_key,p.purpose,p.pool_id,c.recording_id,c.rank,c.score,
                       COALESCE(p.revalidated_policy_version,b.policy_version) AS pool_policy
                FROM recommendation_surface_pools p
                JOIN recommendation_pools b ON b.id=p.pool_id
                CROSS JOIN recommendation_candidates c ON c.pool_id=p.pool_id
                WHERE p.generation_id=:generation AND {pool_scope}
                {catalog_ranks}
            ), subjects AS MATERIALIZED (
                SELECT DISTINCT r.id,r.identity_key,{_OWNED} AS owned
                FROM ranks k CROSS JOIN recordings r ON r.id=k.recording_id
                WHERE r.redirect_recording_id IS NULL AND {eligibility}
            ), uploads AS MATERIALIZED (
                SELECT q.id,COALESCE(MIN(s.id) FILTER (WHERE s.id=r.discovery_entry_id),MIN(s.id)) AS entry_id
                FROM subjects q CROSS JOIN recordings r ON r.id=q.id
                CROSS JOIN track_sources s ON s.recording_id=q.id
                WHERE NOT q.owned AND r.discovery_entry_id IS NOT NULL AND {playable}
                GROUP BY q.id
                {owned_uploads}
            )
            {fit_cte} {heard_cte} {offered_cte}
            SELECT k.mode_key,k.purpose,k.pool_id,k.rank,k.score,k.pool_policy,q.id AS recording_id,
                   q.identity_key AS key,s.video_id,r.effective_title AS title,r.effective_artist AS artist,
                   s.effective_album AS album,s.effective_thumbnail AS thumbnail,s.effective_mb_year AS year,
                   s.effective_duration_s AS duration,COALESCE(g.name,r.effective_unresolved_genre) AS genre,
                   {fit_score} AS intensity_evidence,
                   COALESCE({fit_score},0.5) AS intensity,
                   NOT q.owned AS _radio_fresh,{heard} AS heard,{last_started} AS last_started,
                   {last_offered} AS last_offered
            FROM ranks k CROSS JOIN subjects q ON q.id=k.recording_id
            LEFT JOIN uploads u ON u.id=q.id AND k.purpose<>'catalog'
            CROSS JOIN track_sources s ON s.id=CASE WHEN k.purpose='catalog' THEN k.rank ELSE u.entry_id END
            JOIN recordings r ON r.id=q.id
            LEFT JOIN genre_catalog gd ON gd.key=r.effective_genre_key
            LEFT JOIN genre_catalog g ON g.key=COALESCE(gd.redirect_key,r.effective_genre_key)
            {fit_join} {heard_join} {offered_join}
            WHERE {playable} {fresh_only}
            ORDER BY k.mode_key,k.rank
        ''', params)
        return [dict(row) for row in rows]

    @lookup_read
    def playlist_queue(self, playlist_id):
        """The source queue in saved order, including an empty playlist."""
        rows = list(self.conn.execute(
            'SELECT ' + _METADATA + ' FROM playlist_items q JOIN track_sources e ON e.id=q.track_source_id '
            'JOIN catalog_tracks t ON t.id=e.id WHERE q.playlist_id=? AND e.video_id IS NOT NULL '
            'ORDER BY q.position', (playlist_id,)))
        if not rows and self.conn.execute('SELECT 1 FROM active_playlists WHERE id=?', (playlist_id,)).fetchone() is None:
            return None
        seen = set()
        queue = []
        for row in rows:
            video = row['video_id']
            if video and video not in seen and row['available'] != 0:
                seen.add(video)
                queue.append(dict(row))
        return queue

    @lookup_read
    def launch_seed(self, video):
        seed = self.conn.execute('SELECT ' + _METADATA + ' FROM catalog_tracks t WHERE t.video_id=?',
                                 (video,)).fetchone()
        if seed is None:
            return None
        return self._store.tracks.preferred_playback_uploads([video]).get(video) or dict(seed)

    @lookup_read
    def launch_pools(self, key):
        """Resolve the selected leaf and the shared familiar reserve."""
        rows = self.conn.execute(
            'WITH leaf AS ('
            'SELECT v.external_version FROM group_generations h JOIN group_versions v ON v.generation_id=h.id '
            'WHERE h.is_active=1 AND v.leaf=1 AND v.position IS NOT NULL AND ('
            'EXISTS(SELECT 1 FROM group_members u WHERE u.group_version_id=v.id AND u.raw_key=?) '
            'OR EXISTS(SELECT 1 FROM group_members m WHERE m.group_version_id=v.id '
            'AND m.recording_id=(SELECT id FROM recordings WHERE identity_key=?)))), '
            'lanes(surface,mode_key,purpose) AS ('
            'SELECT \'radio\',external_version,\'membership\' FROM leaf '
            'UNION ALL SELECT \'home\',\'all\',\'wheelhouse\') '
            'SELECT m.surface,m.mode_key,m.generation_id,p.id,p.eligible_count,'
            'COALESCE(m.revalidated_policy_version,p.policy_version) AS policy_version '
            'FROM lanes l JOIN recommendation_generation_surfaces h ON h.selected=1 AND h.surface=l.surface '
            'JOIN recommendation_generations g ON g.id=h.generation_id AND g.status=\'published\' '
            'JOIN recommendation_surface_pools m ON m.generation_id=g.id AND m.surface=h.surface '
            'AND m.mode_key=l.mode_key AND m.purpose=l.purpose '
            'JOIN recommendation_pools p ON p.id=m.pool_id',
            (key, key))
        return {row['surface']: dict(row) for row in rows if row['eligible_count']}

    @lookup_read
    def starting_group_tracks(self, pool, after, limit, wrap):
        """Read one circular window of prepared ranks and resolve playable uploads."""
        representatives = playable_tracks_sql(
            'SELECT c.* FROM ranked r JOIN catalog_tracks c ON c.recording_id=r.recording_id',
            prefer_playback=True)
        return [dict(row) for row in self.conn.execute(
            'WITH ranked AS MATERIALIZED ('
            'SELECT *,0 AS wrapped FROM (SELECT recording_id,rank,score FROM resolved_recommendation_candidates '
            'WHERE pool_id=:pool AND rank>=:after ORDER BY rank LIMIT :limit) '
            'UNION ALL SELECT *,1 AS wrapped FROM (SELECT recording_id,rank,score FROM resolved_recommendation_candidates '
            'WHERE pool_id=:pool AND rank<:after ORDER BY rank LIMIT :wrap)), '
            f'representatives AS MATERIALIZED ({representatives}) '
            'SELECT ' + _METADATA + ',r.rank AS pool_rank,r.score FROM ranked r '
            'JOIN representatives t ON t.recording_id=r.recording_id ORDER BY r.wrapped,r.rank',
            {'pool': pool, 'after': after, 'limit': limit, 'wrap': wrap})]

    @lookup_read
    def rotation_facts(self, keys):
        """Current ownership and any listening, including generated Radio starts.

        Probe only requested identities through recording/time and key/time
        indexes. The taste-learning day-count view deliberately excludes some
        listening and cannot establish that a recording has never been heard.
        """
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        result = {}
        for offset in range(0, len(keys), 128):
            batch = keys[offset:offset + 128]
            marks = ','.join('?' for _ in batch)
            rows = self.conn.execute(
                'SELECT r.identity_key AS key,'
                f'EXISTS({library_recordings_sql("SELECT r.id,r.identity_key")}) AS owned,'
                '(EXISTS(SELECT 1 FROM play_events p WHERE p.recording_id=r.id) OR '
                'EXISTS(SELECT 1 FROM listening_observations o WHERE o.recording_id=r.id) OR '
                'EXISTS(SELECT 1 FROM play_events p WHERE p.recording_id IS NULL AND p.unresolved_subject_key=r.identity_key) OR '
                'EXISTS(SELECT 1 FROM listening_observations o WHERE o.recording_id IS NULL AND o.unresolved_subject_key=r.identity_key) OR '
                "EXISTS(SELECT 1 FROM rec_response_events x WHERE x.identity_key=r.identity_key AND x.kind IN ('start','play'))) AS heard,"
                '(SELECT max(p.played_at) FROM play_events p WHERE p.recording_id=r.id) AS last_played,'
                "(SELECT max(x.at) FROM rec_response_events x WHERE x.identity_key=r.identity_key AND x.kind IN ('start','play')) AS last_response,"
                '(SELECT max(i.served_at) FROM rec_served_impressions i WHERE i.identity_key=r.identity_key '
                f"AND i.surface IN ('radio_seed','radio_directions') AND {_LIVE_IMPRESSION}='live') AS last_offered "
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
        return [genre for genre, in self.conn.execute(
            'SELECT DISTINCT genre FROM catalog_tracks WHERE genre IS NOT NULL ORDER BY genre')]

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
            f'duration_s AS duration,genre,mb_year AS year FROM ({PLAYABLE_LIBRARY_SQL}) '
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
