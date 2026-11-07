"""DiscoveryRepo: the background outward-discovery state: which artists we've scanned (and when),
and the accumulating pools of new albums / new artists we've found, with last-shown bookkeeping.

Tables are created by numbered migrations. The interest signal (which artists are worth
scanning) is computed live from your library: play counts, playlist participation, saved albums.
"""
import json
from contextlib import contextmanager, ExitStack
from yt_playlist.core import album_aliases, album_memberships, discovery_provenance as provenance

from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.core import artist_keys, entity_names, genre_classifications
from yt_playlist.repos import proposal_cache
from yt_playlist.core.preparation.discovery_publication import read_owners

# Audio/content columns persisted per discovered track (mirrors the library `tracks` audio set).
_DTRACK_AUDIO = ("bpm", "energy", "danceability", "mood_happy", "mood_sad", "mood_relaxed",
                 "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
                 "music_key", "music_scale")
_DTRACK_FIELDS = ("id", "recording_id", "identity_key", "video_id", "title", "artist", "album", "thumbnail",
                  "genre", "year", "source_browse_id", "found_at", "first_shown", "last_shown",
                  "offered_count", "last_enriched")
_DTRACK_PROJECTION = ','.join((*_DTRACK_FIELDS, *_DTRACK_AUDIO))

_GENERATED_PL = "(SELECT playlist_scope_id FROM quarantined_playlists)"


class DiscoveryRepo(Repo):
    @snapshot_read
    def track_reassociation_inputs(self, video, previous, target):
        typed = provenance.available(self.conn)
        member = self.conn.execute('SELECT * FROM discovery_memberships WHERE recording_id=?', (previous,)).fetchone()
        remaining = self.conn.execute('SELECT MIN(id) FROM catalog_entries WHERE recording_id=? AND video_id IS NOT ?',
            (previous, video)).fetchone()[0]
        sources = [dict(row) for row in self.conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? ' +
            ('' if remaining is None else 'AND input_video_id=?'), (previous,) if remaining is None else (previous, video))]
        originals = []
        for row in sources:
            predicate = 'origin_id=? AND input_video_id IS ?' if typed else 'source_token=?'
            parameters = (row['origin_id'], row['input_video_id']) if typed else (row['source_token'],)
            collision = self.conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? AND ' + predicate,
                (target, *parameters)).fetchone()
            originals.extend([row] + ([dict(collision)] if collision else []))
        return dict(typed=typed, previous=previous, target=target, video=video, remaining=remaining,
            member=dict(member) if member else None, sources=sources, originals=originals,
            impressions=[dict(row) for row in self.conn.execute(
                'SELECT * FROM discovery_impressions WHERE recording_id IN (?,?)', (previous, target))] if typed and remaining is None else [],
            legacy=[dict(row) for row in self.conn.execute(
                'SELECT * FROM discovery_legacy_offers WHERE recording_id IN (?,?)', (previous, target))] if typed and remaining is None else [])

    @synchronized
    def finish_track_reassociation(self, inputs, *, retain_member, membership_found, source_identities, association=None):
        previous, target, video = inputs['previous'], inputs['target'], inputs['video']
        with self.conn.atomic(join=True):
            member, remaining = inputs['member'], inputs['remaining']
            if retain_member:
                entry = self.conn.execute('SELECT id FROM catalog_entries WHERE video_id=?', (video,)).fetchone()[0]
                if inputs['typed']:
                    self.conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) '
                        'ON CONFLICT(recording_id) DO UPDATE SET found_at=min(coalesce(found_at,excluded.found_at),'
                        'coalesce(excluded.found_at,found_at))', (target, entry, membership_found))
                elif remaining is None:
                    from yt_playlist.core.discovery_sources import merge_membership
                    merge_membership(self.conn, target, {**member, 'entry_id': entry})
                else:
                    self.conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) '
                        'ON CONFLICT(recording_id) DO NOTHING', (target, entry, membership_found))
            for row, identity in zip(inputs['sources'], source_identities, strict=True):
                if inputs['typed']:
                    provenance.retain(self.conn, target, row['origin_id'], row['input_video_id'],
                        row['last_found_at'], first_found_at=row['first_found_at'])
                    self.conn.execute('DELETE FROM discovery_sources WHERE id=?', (row['id'],))
                else:
                    self.conn.execute('INSERT INTO discovery_sources(recording_id,source_token,input_video_id,found_at,'
                        'source_type,source_id,last_found_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(recording_id,source_token) '
                        'DO UPDATE SET found_at=min(coalesce(found_at,excluded.found_at),coalesce(excluded.found_at,found_at)),'
                        'last_found_at=max(coalesce(last_found_at,excluded.last_found_at),coalesce(excluded.last_found_at,last_found_at))',
                        (target, row['source_token'], row['input_video_id'], row['found_at'], *identity, row['last_found_at']))
                    self.conn.execute('DELETE FROM discovery_sources WHERE recording_id=? AND source_token=?',
                        (previous, row['source_token']))
            if inputs['typed']:
                for row in inputs['impressions']:
                    if row['recording_id'] == previous:
                        self.conn.execute('INSERT INTO discovery_impressions(view_id,recording_id,shown_at) VALUES (?,?,?) '
                            'ON CONFLICT(view_id,recording_id) WHERE recording_id IS NOT NULL '
                            'DO UPDATE SET shown_at=min(shown_at,excluded.shown_at)', (row['view_id'], target, row['shown_at']))
                for row in inputs['legacy']:
                    if row['recording_id'] == previous:
                        provenance.retain_legacy(self.conn, 'track', target, row['offered_count'], row['first_shown'], row['last_shown'])
                if remaining is None:
                    self.conn.execute('DELETE FROM discovery_impressions WHERE recording_id=?', (previous,))
                    self.conn.execute('DELETE FROM discovery_legacy_offers WHERE recording_id=?', (previous,))
                provenance.refresh_counts(self.conn, 'track', target)
            from yt_playlist.core.association_work import complete
            complete(self.conn, association, 'discovery')


    @snapshot_read
    def artist_reconciliation_inputs(self, previous, target):
        typed = provenance.available(self.conn)
        rows = {table: [dict(row) for row in self.conn.execute(
            'SELECT * FROM '+table+' WHERE artist_id IN (?,?)', (previous, target))]
            for table in ('artist_scans', 'discovered_artists')}
        inputs = {'typed': typed, **rows, 'duplicates': 0,
                  'sources': [], 'impressions': [], 'legacy': []}
        if typed:
            inputs['sources'] = [dict(row) for row in self.conn.execute(
                'SELECT * FROM discovery_artist_sources WHERE artist_id IN (?,?)', (previous, target))]
            inputs['impressions'] = [dict(row) for row in self.conn.execute(
                'SELECT * FROM discovery_impressions WHERE artist_id IN (?,?)', (previous, target))]
            inputs['legacy'] = [dict(row) for row in self.conn.execute(
                'SELECT * FROM discovery_legacy_offers WHERE artist_id IN (?,?)', (previous, target))]
        else:
            lifecycles = {row['artist_id']: row['first_shown'] for row in rows['discovered_artists']}
            inputs['duplicates'] = self.conn.execute(
                'SELECT COUNT(*) FROM discovery_visible_impressions a '
                'JOIN discovery_visible_impressions b ON a.view_id=b.view_id AND a.kind=b.kind '
                "WHERE a.kind='artist' AND a.item_id=? AND b.item_id=? AND a.shown_at>=? AND b.shown_at>=?",
                (str(previous), str(target), lifecycles.get(previous), lifecycles.get(target))).fetchone()[0]
        return inputs

    @snapshot_read
    def origin_merge_inputs(self, column, previous, target):
        if column not in ('artist_id', 'album_id', 'genre_key'):
            raise ValueError('Unsupported discovery origin identity')
        if not provenance.available(self.conn):
            return []
        inputs = []
        for row in self.conn.execute('SELECT * FROM discovery_origins WHERE '+column+'=?', (previous,)):
            current = self.conn.execute('SELECT id FROM discovery_origins WHERE kind=? AND '+column+'=?',
                                        (row['kind'], target)).fetchone()
            inputs.append({'origin': dict(row), 'target': current[0] if current else None,
                'sources': [dict(source) for source in self.conn.execute(
                    'SELECT * FROM discovery_sources WHERE origin_id=?', (row['id'],))] if current else [],
                'artists': [dict(source) for source in self.conn.execute(
                    'SELECT * FROM discovery_artist_sources WHERE origin_id=?', (row['id'],))]})
        return inputs

    @synchronized
    def apply_origin_merge(self, column, target, origins):
        if column not in ('artist_id', 'album_id', 'genre_key'):
            raise ValueError('Unsupported discovery origin identity')
        with self.conn.atomic(join=True):
            self._move_origins(column, target, origins)

    def _move_origins(self, column, target, origins):
        for item in origins:
            old, current = item['origin']['id'], item['target']
            if current is None:
                self.conn.execute('UPDATE discovery_origins SET '+column+'=? WHERE id=?', (target, old))
            else:
                for row in item['sources']:
                    provenance.retain(self.conn, row['recording_id'], current, row['input_video_id'],
                        row['last_found_at'], first_found_at=row['first_found_at'])
                for row in item['artists']:
                    provenance.retain_artist(self.conn, row['artist_id'], current, row['score'])
                self.conn.execute('DELETE FROM discovery_sources WHERE origin_id=?', (old,))
                self.conn.execute('DELETE FROM discovery_artist_sources WHERE origin_id=?', (old,))
                self.conn.execute('DELETE FROM discovery_origins WHERE id=?', (old,))
            for row in item['artists']:
                provenance.invalidate_explanation(self.conn, row['artist_id'])

    @synchronized
    def apply_artist_reconciliation(self, previous, target, inputs, origins, scan, candidate):
        with self.conn.atomic(join=True):
            if inputs['typed']:
                for row in inputs['sources']:
                    if row['artist_id'] == previous:
                        provenance.retain_artist(self.conn, target, row['origin_id'], row['score'])
                self.conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=?', (previous,))
                self._move_origins('artist_id', target, origins)
            elif self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_artist_sources'").fetchone():
                if not self.conn.execute('SELECT 1 FROM discovered_artists WHERE artist_id=?', (target,)).fetchone():
                    self.conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=?', (target,))
                columns = {row[1] for row in self.conn.execute('PRAGMA table_info(discovery_artist_sources)')}
                account = 'account_id' if 'account_id' in columns else 'identity_id'
                self.conn.execute(f'INSERT INTO discovery_artist_sources SELECT ?,s.source,s.{account},s.score '
                    'FROM discovery_artist_sources s JOIN discovered_artists d ON d.artist_id=s.artist_id '
                    'WHERE s.artist_id=? ON CONFLICT(artist_id,source) DO UPDATE SET score=MAX(score,excluded.score)',
                    (target, previous))
                self.conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=?', (previous,))
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_artist_image_state'").fetchone():
                self.conn.execute('INSERT INTO discovery_artist_image_state(artist_id,retry_at) SELECT ?,retry_at '
                    'FROM discovery_artist_image_state WHERE artist_id=? ON CONFLICT(artist_id) DO UPDATE SET '
                    'retry_at=MAX(retry_at,excluded.retry_at)', (target, previous))
                self.conn.execute('DELETE FROM discovery_artist_image_state WHERE artist_id=?', (previous,))
            for table, row in (('artist_scans', scan), ('discovered_artists', candidate)):
                if row is not None:
                    self.conn.execute('DELETE FROM '+table+' WHERE artist_id IN (?,?)', (previous, target))
                    self.conn.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES ('+
                                      ','.join('?' for _ in row)+')', tuple(row.values()))
            if inputs['typed']:
                for row in inputs['impressions']:
                    if row['artist_id'] == previous:
                        self.conn.execute('INSERT INTO discovery_impressions(view_id,artist_id,shown_at) VALUES (?,?,?) '
                            'ON CONFLICT(view_id,artist_id) WHERE artist_id IS NOT NULL DO UPDATE '
                            'SET shown_at=MIN(shown_at,excluded.shown_at)', (row['view_id'], target, row['shown_at']))
                self.conn.execute('DELETE FROM discovery_impressions WHERE artist_id=?', (previous,))
                for row in inputs['legacy']:
                    if row['artist_id'] == previous:
                        provenance.retain_legacy(self.conn, 'artist', target, row['offered_count'], row['first_shown'], row['last_shown'])
                self.conn.execute('DELETE FROM discovery_legacy_offers WHERE artist_id=?', (previous,))
                provenance.refresh_counts(self.conn, 'artist', target)
                provenance.invalidate_explanation(self.conn, target)
            else:
                self.conn.execute("INSERT INTO discovery_visible_impressions SELECT view_id,'artist',?,shown_at "
                    "FROM discovery_visible_impressions WHERE kind='artist' AND item_id=? "
                    'ON CONFLICT(view_id,kind,item_id) DO UPDATE SET shown_at=MIN(shown_at,excluded.shown_at)',
                    (str(target), str(previous)))
                self.conn.execute("DELETE FROM discovery_visible_impressions WHERE kind='artist' AND item_id=?", (str(previous),))

    @snapshot_read
    def album_merge_inputs(self, previous, target):
        typed = provenance.available(self.conn)
        return {'typed': typed, 'memberships': [dict(row) for row in self.conn.execute(
            'SELECT * FROM album_discovery_memberships WHERE album_id IN (?,?)', (previous, target))],
            **{key: [dict(row) for row in self.conn.execute('SELECT * FROM '+table+' WHERE album_id IN (?,?)',
                (previous, target))] if typed else [] for key, table in (
                    ('impressions', 'discovery_impressions'), ('legacy', 'discovery_legacy_offers'))}}

    @synchronized
    def apply_album_merge(self, previous, target, inputs, origins, member):
        with self.conn.atomic():
            self.conn.execute('DELETE FROM album_discovery_memberships WHERE album_id IN (?,?)', (previous, target))
            if member is not None:
                self.conn.execute('INSERT INTO album_discovery_memberships('+','.join(member)+') VALUES ('+
                                  ','.join('?' for _ in member)+')', tuple(member.values()))
            if inputs['typed']:
                self._move_origins('album_id', target, origins)
                self.conn.execute('INSERT INTO discovery_impressions(view_id,album_id,shown_at) '
                    'SELECT view_id,?,shown_at FROM discovery_impressions WHERE album_id=? '
                    'ON CONFLICT(view_id,album_id) WHERE album_id IS NOT NULL DO UPDATE '
                    'SET shown_at=MIN(shown_at,excluded.shown_at)', (target, previous))
                self.conn.execute('DELETE FROM discovery_impressions WHERE album_id=?', (previous,))
                for row in inputs['legacy']:
                    if row['album_id'] == previous:
                        provenance.retain_legacy(self.conn, 'album', target,
                            row['offered_count'], row['first_shown'], row['last_shown'])
                self.conn.execute('DELETE FROM discovery_legacy_offers WHERE album_id=?', (previous,))
                provenance.refresh_counts(self.conn, 'album', target)

    @synchronized
    def remove_spotify_origins(self):
        from yt_playlist.core.discovery_sources import remove_spotify_artists
        with self.conn.atomic():
            return remove_spotify_artists(self.conn)

    @lookup_read
    def fetched_albums(self, browse_ids):
        """Detach retained fetch checkpoints without borrowing another writer's transaction."""
        from yt_playlist.core import discovery_checkpoints
        return {browse for browse in browse_ids if discovery_checkpoints.fetched(self.conn, browse)}

    @synchronized
    def remember_album_fetch(self, browse):
        from yt_playlist.core import discovery_checkpoints
        with self.conn:
            discovery_checkpoints.remember(self.conn, browse)

    @property
    def _artist_relation(self):
        return read_owners(self.conn).relation('discovered_artists')

    @snapshot_read
    def starter_artists(self):
        """Explicit artist choices, independent of imported plays, likes and ownership."""
        # #620: retain installed source reads only while pre-v101 admission is supported.
        owners = read_owners(self.conn)
        if not owners.typed:
            return [dict(row) for row in self.conn.execute(
                'SELECT a.id artist_id,a.name name,p.provider_id browse_id,d.thumbnail '
                'FROM discovery_artist_sources s JOIN artists a ON a.id=s.artist_id '
                "JOIN artist_provider_ids p ON p.artist_id=a.id AND p.provider='youtube' "
                f'LEFT JOIN {self._artist_relation} d ON d.artist_id=a.id '
                "WHERE s.source='artist_start:'||p.provider_id ORDER BY a.id,p.provider_id")]
        return [dict(row) for row in self.conn.execute(
            'SELECT a.id artist_id,a.name name,p.provider_id browse_id,d.thumbnail '
            f"FROM {owners.relation('discovery_artist_sources')} s JOIN artists a ON a.id=s.artist_id "
            f"JOIN {owners.relation('discovery_origins')} o ON o.id=s.origin_id "
            "JOIN artist_provider_ids p ON p.artist_id=a.id AND p.provider='youtube' "
            f"LEFT JOIN {owners.relation('discovered_artists')} d ON d.artist_id=a.id "
            "WHERE o.kind='artist_start' AND o.artist_id=a.id ORDER BY a.id,p.provider_id")]

    @lookup_read
    def starter_count(self, excluding):
        starter = ("origin_id IN (SELECT id FROM discovery_origins WHERE kind='artist_start')"
                   if provenance.available(self.conn) else "source LIKE 'artist_start:%'")
        return self.conn.execute('SELECT COUNT(DISTINCT artist_id) FROM discovery_artist_sources '
                                 'WHERE ' + starter + ' AND artist_id<>?', (excluding,)).fetchone()[0]

    @synchronized
    def set_starter_artist(self, aid, browse_id, thumbnail, selected, now, *, score, maximum, source, origin):
        with self.conn:
            typed = provenance.available(self.conn)
            starter = ("origin_id IN (SELECT id FROM discovery_origins WHERE kind='artist_start')" if typed else
                       "source LIKE 'artist_start:%'")
            if selected:
                count = self.conn.execute("SELECT COUNT(DISTINCT s.artist_id) FROM discovery_artist_sources s "
                    'WHERE '+starter+' AND s.artist_id<>?', (aid,)).fetchone()[0]
                if count >= maximum:
                    raise ValueError('Choose up to 12 artists to start.')
                self.conn.execute('DELETE FROM main.discovery_artist_sources WHERE artist_id=? AND '+starter, (aid,))
                self._store_artist_source(aid, score, source, origin)
                self.conn.execute(
                    'INSERT INTO discovered_artists(artist_id,score,thumbnail,found_at'+('' if typed else ',artist')+') '
                    'VALUES (?,?,?,?'+('' if typed else ',?')+') ON CONFLICT(artist_id) DO UPDATE SET '
                    "thumbnail=COALESCE(NULLIF(excluded.thumbnail,''),discovered_artists.thumbnail)",
                    (aid, score, thumbnail, now) + (() if typed else (f'@artist:{aid}',)))
                provenance.refresh_counts(self.conn,'artist',aid)
            else:
                self.conn.execute('DELETE FROM main.discovery_artist_sources WHERE artist_id=? AND '+starter, (aid,))
                provenance.invalidate_explanation(self.conn,aid)

    @snapshot_read
    def starter_track_rows(self, maximum_duration):
        """Only retained, available songs from the currently selected provider identities."""
        length_clause = '(c.duration_s IS NULL OR c.duration_s <= ?)'
        owners = read_owners(self.conn)
        if not owners.typed:
            rows = self.conn.execute(
                'SELECT DISTINCT c.identity_key AS key,c.video_id,c.title,c.artist,c.album,c.thumbnail,'
                'c.duration_s AS duration,p.provider_id AS seed,c.recording_id FROM discovery_sources s '
                'JOIN catalog_tracks c ON c.recording_id=s.recording_id AND c.video_id=s.input_video_id '
                "JOIN artist_provider_ids p ON p.provider='youtube' AND s.source_token='artist:'||p.provider_id "
                'JOIN discovery_artist_sources a ON a.artist_id=p.artist_id '
                "WHERE a.source='artist_start:'||p.provider_id "
                "AND c.video_id IS NOT NULL AND c.title<>'' AND COALESCE(c.available,1)<>0 "
                f'AND {length_clause} '
                'ORDER BY p.artist_id,s.found_at,c.recording_id', (maximum_duration,)).fetchall()
        else:
            rows = self.conn.execute(
                'SELECT DISTINCT c.identity_key AS key,c.video_id,c.title,c.artist,c.album,c.thumbnail,'
                f"c.duration_s AS duration,p.provider_id AS seed,c.recording_id FROM {owners.relation('discovery_sources')} s "
                f"JOIN {owners.relation('discovery_origins')} o ON o.id=s.origin_id "
                'JOIN catalog_tracks c ON c.recording_id=s.recording_id AND c.video_id=s.input_video_id '
                "JOIN artist_provider_ids p ON p.provider='youtube' AND p.artist_id=o.artist_id "
                f"JOIN {owners.relation('discovery_artist_sources')} a ON a.artist_id=p.artist_id "
                f"JOIN {owners.relation('discovery_origins')} starter ON starter.id=a.origin_id "
                "WHERE o.kind='artist' AND starter.kind='artist_start' AND starter.artist_id=o.artist_id "
                "AND c.video_id IS NOT NULL AND c.title<>'' AND COALESCE(c.available,1)<>0 "
                f'AND {length_clause} '
                'ORDER BY p.artist_id,s.first_found_at,c.recording_id', (maximum_duration,)).fetchall()
        return [dict(row) for row in rows]

    @snapshot_read
    def interested_artists(self, *, weights, limit=None) -> list:
        """Engaged artists, highest interest first. `limit` (#52) keeps only the top-N so discovery
        scans a focused set instead of every artist you have ever touched."""
        owners = read_owners(self.conn)
        seeds = (f"JOIN {owners.relation('discovery_origins')} o ON o.id=s.origin_id WHERE o.kind='artist_start'" if owners.typed
                 else "WHERE s.source LIKE 'artist_start:%'")
        rows = self.conn.execute(
            "WITH days AS MATERIALIZED (SELECT identity_key,COUNT(*) n FROM listening_daily_items "
            "                          GROUP BY identity_key), "
            "     credits AS MATERIALIZED (SELECT DISTINCT r.identity_key,ta.artist_id "
            "         FROM catalog_entries e JOIN library_memberships m ON m.entry_id=e.id "
            f"         JOIN recordings r ON r.id=e.recording_id JOIN {self.entry_artists_table} ta ON ta.entry_id=e.id), "
            "     plays AS (SELECT c.artist_id a,SUM(d.n) n FROM days d "
            "               JOIN credits c ON c.identity_key=d.identity_key GROUP BY c.artist_id), "
            "     pls AS (SELECT ta.artist_id a, COUNT(DISTINCT pt.playlist_id) n "
            "               FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            f"               JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id "
            "               JOIN playlists p ON p.id=pt.playlist_id "
            f"              WHERE p.playlist_scope_id NOT IN {_GENERATED_PL} "
            "               GROUP BY ta.artist_id), "
            f"     sv AS (SELECT aa.artist_id a,COUNT(DISTINCT m.album_id) n FROM {album_memberships.read_relation(self.conn)} m "
            "            JOIN album_artists aa ON aa.album_id=m.album_id GROUP BY aa.artist_id), "
            f"     seeds AS (SELECT s.artist_id a,MAX(s.score) n FROM {owners.relation('discovery_artist_sources')} s "
            + seeds + " GROUP BY s.artist_id), "
            "     arts AS (SELECT a FROM plays UNION SELECT a FROM pls UNION SELECT a FROM sv UNION SELECT a FROM seeds) "
            "SELECT a.name artist,a.id artist_id,ap.provider_id browse_id, "
            "       COALESCE(plays.n,0)*? + COALESCE(pls.n,0)*? + COALESCE(sv.n,0)*? + COALESCE(seeds.n,0) score "
            "FROM arts JOIN artists a ON a.id=arts.a LEFT JOIN artist_provider_ids ap "
            "ON ap.artist_id=a.id AND ap.provider='youtube' "
            "LEFT JOIN plays ON plays.a=arts.a LEFT JOIN pls ON pls.a=arts.a "
            "LEFT JOIN sv ON sv.a=arts.a LEFT JOIN seeds ON seeds.a=arts.a ORDER BY score DESC"
            + (" LIMIT ?" if limit is not None else ""),
            (*weights, limit) if limit is not None else tuple(weights)).fetchall()
        return [dict(r) for r in rows]

    @snapshot_read
    def artist_scan_times(self, artist_ids):
        result = {}
        for start in range(0, len(artist_ids), 500):
            batch = artist_ids[start:start + 500]
            result.update(self.conn.execute('SELECT artist_id,scanned_at FROM '
                + read_owners(self.conn).relation('artist_scans') + ' WHERE artist_id IN ('
                + ','.join('?' for _ in batch) + ')', batch))
        return result

    @snapshot_read
    def discovered_albums_for_artist(self, aid) -> dict:
        """{browse_id: offered_count} for one artist's pooled albums (#52 rotation input)."""
        return {r["browse_id"]: (r["offered_count"] or 0) for r in self.conn.execute(
            "SELECT browse_id, offered_count FROM "+read_owners(self.conn).relation('discovered_albums')+" WHERE artist_id=?", (aid,))}

    @synchronized
    def delete_discovered_albums(self, browse_ids) -> int:
        """Delete the given pooled albums (rotation / bounds enforcement). Returns the count removed."""
        ids = list({album_aliases.primary_id(self.conn, bid) for bid in browse_ids})
        if not ids:
            return 0
        self.conn.executemany("DELETE FROM discovered_albums WHERE browse_id=?", [(b,) for b in ids])
        self.conn.commit()
        return len(ids)

    @snapshot_read
    def orphan_track_keys(self):
        """Drop discovered tracks whose source album is no longer in the pool (#52 cleanup). Radio-
        and genre-sourced tracks have their own lifecycle and are kept. Returns the
        count removed."""
        album_source = ("JOIN discovery_origins o ON o.id=s.origin_id WHERE "
                        "s.recording_id=d.recording_id AND (o.kind<>'album' "
                        "OR o.album_id IN (SELECT album_id FROM album_discovery_memberships))"
                        if provenance.available(self.conn) else
                        "WHERE s.recording_id=d.recording_id AND (s.source_type<>'album' OR "
                        "s.source_token IN (SELECT browse_id FROM discovered_albums))")
        rows = self.conn.execute(
            "SELECT r.identity_key FROM discovery_memberships d JOIN recordings r ON r.id=d.recording_id "
            "WHERE EXISTS(SELECT 1 FROM discovery_sources s WHERE s.recording_id=d.recording_id) "
            'AND NOT EXISTS(SELECT 1 FROM discovery_sources s '+album_source+')').fetchall()
        return {row['identity_key'] for row in rows}

    @synchronized
    def mark_scanned(self, aid, now) -> None:
        typed = provenance.available(self.conn)
        self.conn.execute('INSERT INTO artist_scans(artist_id,scanned_at'+('' if typed else ',artist')+') VALUES (?,?'+
                          ('' if typed else ',?')+') ON CONFLICT(artist_id) DO UPDATE SET scanned_at=excluded.scanned_at',
                          (aid,now) + (() if typed else (f'@artist:{aid}',)))
        self.conn.commit()

    # --- the accumulating pools ---
    @synchronized
    def add_album_membership(self, album_id, now):
        with self.conn:
            self.conn.execute('INSERT INTO album_discovery_memberships(album_id,found_at) VALUES (?,?) ON CONFLICT(album_id) DO NOTHING', (album_id,now))
            provenance.refresh_counts(self.conn,'album',album_id)

    @snapshot_read
    def origin_inputs(self, value, account):
        video = self.conn.execute('SELECT video_id FROM youtube_items WHERE video_id=?', (value,)).fetchone()
        album = self.conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?", (value,)).fetchone()
        return dict(video=video[0] if video else None, album=album[0] if album else None,
            account=self.conn.execute(f'SELECT 1 FROM {entity_names.accounts(self.conn)} WHERE id=?', (account,)).fetchone() is not None,
            artists={artist_keys.canonical_id(self.conn, row[0]) for row in self.conn.execute(
                "SELECT artist_id FROM artist_provider_ids WHERE provider='youtube' AND provider_id=?", (value,))})

    @lookup_read
    def explanation_files(self):
        return provenance.available(self.conn)

    @synchronized
    def store_discovered_artist(self, aid, score, thumbnail, now, genre, *, source, reference, encoded, origin, genre_key):
        with self.conn:
            typed = provenance.available(self.conn)
            self._store_artist_source(aid, score, source, origin)
            self.conn.execute(
                'INSERT INTO discovered_artists(artist_id,score,thumbnail,found_at'+('' if typed else ',artist')+') '
                'VALUES (?,?,?,?'+('' if typed else ',?')+') ON CONFLICT(artist_id) DO UPDATE SET score=excluded.score, '
                "thumbnail=COALESCE(NULLIF(excluded.thumbnail,''), discovered_artists.thumbnail)",
                (aid,score,thumbnail,now) + (() if typed else (f'@artist:{aid}',)))
            if typed:
                self.conn.execute('UPDATE discovered_artists SET recovery_filename=?,recovery_digest=?,recovery_bytes=? '
                                  'WHERE artist_id=?', (*reference, aid))
            else:
                self.conn.execute('UPDATE main.discovered_artists SET because=?,fits=? WHERE artist_id=?', (*encoded, aid))
            provenance.refresh_counts(self.conn,'artist',aid)
            if genre is not None:
                genre_classifications.write_prepared(self.conn, 'discovered_artists', aid, genre, genre_key)

    @lookup_read
    def artist_images_due(self, artist_ids, now):
        """Only the bounded on-screen cohort, with a day between failed lookups."""
        ids = list(artist_ids)
        if not ids:
            return []
        qs = ','.join('?' for _ in ids)
        typed = self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_artist_image_state'").fetchone()
        from yt_playlist.core.discovery_checkpoint_reads import OWNERS
        owner = OWNERS['artist-images']
        revision = owner.selected(self.conn) if not typed else None
        if revision is not None:
            join = ('LEFT JOIN '+owner.table('rows')+" s ON s.stage=? AND s.source='discovery:artist-image:' || d.artist_id "
                    'AND s.artist_id=d.artist_id ')
            parameters = [revision, *ids, now]
        else:
            join = ('LEFT JOIN discovery_artist_image_state s ON s.artist_id=d.artist_id ' if typed else
                    "LEFT JOIN settings s ON s.key='discovery:artist-image:' || d.artist_id ")
            parameters = [*ids, now]
        deadline = 's.retry_at' if typed or revision is not None else 'CAST(s.value AS REAL)'
        return [dict(r) for r in self.conn.execute(
            f'SELECT d.artist_id,a.name artist FROM {self._artist_relation} d '
            'JOIN artists a ON a.id=d.artist_id '+join+
            f"WHERE d.artist_id IN ({qs}) AND COALESCE(d.thumbnail,'')='' "
            f'AND COALESCE({deadline},0)<=? ORDER BY d.artist_id', parameters)]

    @synchronized
    def record_artist_image(self, artist_id, thumbnail, *, retry_at):
        """An image lookup cannot overwrite rankings, provenance or exposure counts."""
        with self.conn:
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_artist_image_state'").fetchone():
                self.conn.execute('INSERT INTO discovery_artist_image_state(artist_id,retry_at) VALUES (?,?) '
                    'ON CONFLICT(artist_id) DO UPDATE SET retry_at=excluded.retry_at',(artist_id,retry_at))
            else:
                self.conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (f'discovery:artist-image:{artist_id}', str(retry_at)))
            if thumbnail:
                self.conn.execute("UPDATE discovered_artists SET thumbnail=? WHERE artist_id=? "
                                  "AND COALESCE(thumbnail,'')=''", (thumbnail, artist_id))

    @synchronized
    def retain_discovered_artist(self, aid, score, *, source, origin, excluded_kinds):
        """Retain independent interest without overwriting an existing suggestion's metadata."""
        if self.conn.execute(
                'SELECT 1 FROM discovered_artists WHERE artist_id=?', (aid,)).fetchone():
            with self.conn:
                if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_artist_sources'").fetchone():
                    return
                independent = self._other_source_predicate(excluded_kinds)
                self.conn.execute('UPDATE discovered_artists SET score=? WHERE artist_id=? '
                    'AND NOT EXISTS (SELECT 1 FROM discovery_artist_sources WHERE artist_id=? '
                    'AND '+independent+')', (score, aid, aid, *excluded_kinds))
                self._store_artist_source(aid, score, source, origin)

    def _other_source_predicate(self, excluded_kinds):
        slots = ','.join('?' for _ in excluded_kinds)
        if provenance.available(self.conn):
            return f'origin_id IN (SELECT id FROM discovery_origins WHERE kind NOT IN ({slots}))'
        return ("CASE WHEN instr(source,':')>0 THEN substr(source,1,instr(source,':')-1) ELSE source END "
                f'NOT IN ({slots})')

    def _store_artist_source(self, aid, score, source, origin, account_id=None):
        if provenance.available(self.conn):
            provenance.retain_artist(self.conn, aid, provenance.store_origin(self.conn, origin), score)
        elif self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_artist_sources'").fetchone():
            if not self.conn.execute('SELECT 1 FROM discovered_artists WHERE artist_id=?', (aid,)).fetchone():
                self.conn.execute('DELETE FROM discovery_artist_sources WHERE artist_id=?', (aid,))
            self.conn.execute('INSERT INTO discovery_artist_sources VALUES (?,?,?,?) '
                'ON CONFLICT(artist_id,source) DO UPDATE SET score=MAX(score,excluded.score)', (aid, source, account_id, score or 0))

    @synchronized
    def seed_spotify_artists(self, account_id, rows, now, *, source, origin, excluded_kinds):
        """Retain import provenance even when an artist was independently discovered."""
        seeded = 0
        typed = provenance.available(self.conn)
        with self.conn:
            for aid, plays in rows:
                self._store_artist_source(aid, plays, source, origin, account_id)
                seeded += self.conn.execute(
                    'INSERT INTO discovered_artists(artist_id,score,found_at'+('' if typed else ',artist')+') '
                    'VALUES (?,?,?'+('' if typed else ',?')+') ON CONFLICT(artist_id) DO NOTHING',
                    (aid,float(plays),now) + (() if typed else (f'@artist:{aid}',))).rowcount
                independent = self._other_source_predicate(excluded_kinds)
                self.conn.execute('UPDATE discovered_artists SET score=('
                    'SELECT MAX(score) FROM discovery_artist_sources WHERE artist_id=?) WHERE artist_id=? '
                    'AND NOT EXISTS (SELECT 1 FROM discovery_artist_sources WHERE artist_id=? '
                    'AND '+independent+')', (aid, aid, aid, *excluded_kinds))
                provenance.refresh_counts(self.conn,'artist',aid)
        return seeded

    @snapshot_read
    def get_discovered_albums(self) -> list:
        return [{"browse_id": r["browse_id"], "artist": r["artist"], "artist_id":r['artist_id'], "title": r["title"],
                 "year": r["year"], "thumbnail": r["thumbnail"], "found_at": r["found_at"],
                 "last_shown": r["last_shown"], "genre": r["genre"],
                 "first_shown": r["first_shown"], "offered_count": r["offered_count"]}
                for r in self.conn.execute("SELECT * FROM "+read_owners(self.conn).relation('discovered_albums'))]

    @snapshot_read
    def present_home_tiles(self, album_ids, artist_ids):
        """Check a bounded prepared selection without reading/scoring either full pool."""
        albums, artists_found = {}, {}
        owners = read_owners(self.conn)
        if album_ids:
            aliases = {bid: album_aliases.primary_id(self.conn, bid) for bid in album_ids}
            qs = ",".join("?" for _ in album_ids)
            albums = {r["browse_id"]: dict(r) for r in self.conn.execute(
                f"SELECT browse_id,artist,title,genre,year,thumbnail FROM {owners.relation('discovered_albums')} WHERE browse_id IN ({qs})", list(aliases.values()))}
            albums = {bid: {**albums[preferred], 'browse_id': bid}
                      for bid, preferred in aliases.items() if preferred in albums}
        if artist_ids:
            qs = ",".join("?" for _ in artist_ids)
            artists_found = {r["artist_id"]: dict(r) for r in self.conn.execute(
                f"SELECT d.artist_id,a.name artist,{self._artist_genre} AS genre,d.thumbnail FROM {owners.relation('discovered_artists')} d "
                f"JOIN artists a ON a.id=d.artist_id WHERE d.artist_id IN ({qs})", list(artist_ids))}
        return albums, artists_found

    @snapshot_read
    def discovered_artist_rows(self) -> list:
        return [dict(row) for row in self.conn.execute(
            f"SELECT {self._artist_genre} AS genre,d.*,a.name display_artist FROM {self._artist_relation} d JOIN artists a ON a.id=d.artist_id")]

    @property
    def _artist_genre(self):
        return genre_classifications.read_expression(self.conn, 'd', 'discovered_artists')

    @synchronized
    def add_track_membership(self, rid, entry_id, now):
        with self.conn:
            self.conn.execute(
                "INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) "
                "ON CONFLICT(recording_id) DO NOTHING", (rid,entry_id,now))
            provenance.refresh_counts(self.conn,'track',rid)

    @lookup_read
    def pooled_album(self, browse):
        return self.conn.execute('SELECT 1 FROM discovered_albums WHERE browse_id=?',
            (album_aliases.primary_id(self.conn, browse),)).fetchone() is not None

    @synchronized
    def retain_track_source(self, rid, token, kind, identifier, video, now, origin):
        with self.conn:
            if provenance.available(self.conn):
                provenance.retain(self.conn, rid, provenance.store_origin(self.conn, origin), video, now)
            else:
                self.conn.execute('INSERT INTO discovery_sources(recording_id,source_token,input_video_id,found_at,'
                    'source_type,source_id,last_found_at) VALUES (?,?,?,?,?,?,?) '
                    'ON CONFLICT(recording_id,source_token) DO UPDATE SET '
                    'found_at=MIN(COALESCE(found_at,excluded.found_at),COALESCE(excluded.found_at,found_at)),'
                    'last_found_at=MAX(COALESCE(last_found_at,excluded.last_found_at),COALESCE(excluded.last_found_at,last_found_at))',
                    (rid, token, video, now, kind, identifier, now))

    @snapshot_read
    def discovery_sources_for_track(self, key) -> list:
        owners = read_owners(self.conn)
        if not owners.typed:
            return [dict(row) for row in self.conn.execute(
                'SELECT s.* FROM discovery_sources s JOIN recordings r ON r.id=s.recording_id '
                'WHERE r.identity_key=? ORDER BY s.found_at,s.source_token', (key,))]
        return [dict(row) for row in self.conn.execute(
            "SELECT s.*,s.first_found_at found_at,t.source_token,o.kind source_type,"
            "CASE o.kind WHEN 'radio' THEN o.seed_video_id WHEN 'genre' THEN o.genre_key "
            "WHEN 'album' THEN substr(t.source_token,1) WHEN 'artist' THEN substr(t.source_token,8) "
            "ELSE t.source_token END source_id,o.resolution_status,o.resolution_reason "
            f"FROM {owners.relation('discovery_sources')} s JOIN recordings r ON r.id=s.recording_id "
            f"JOIN {owners.relation('discovery_origins')} o ON o.id=s.origin_id JOIN {owners.relation('discovery_origin_tokens')} t ON t.id=o.id "
            "WHERE r.identity_key=? ORDER BY s.first_found_at,t.source_token,s.id", (key,))]

    @snapshot_read
    def discovered_source_tokens(self, source_type=None) -> set:
        owners = read_owners(self.conn)
        if not owners.typed:
            return {row[0] for row in self.conn.execute('SELECT DISTINCT source_token FROM discovery_sources'+
                (' WHERE source_type=?' if source_type else ''), (source_type,) if source_type else ())}
        return {row[0] for row in self.conn.execute(
            f"SELECT DISTINCT t.source_token FROM {owners.relation('discovery_sources')} s JOIN {owners.relation('discovery_origin_tokens')} t ON t.id=s.origin_id"
            + (" WHERE t.kind=?" if source_type else ""),
            (source_type,) if source_type else ())}

    @contextmanager
    def cleanup_snapshot(self):
        if self._lock.held_by_current_thread:
            raise RuntimeError('Discovery cleanup must prepare outside the serving lock')
        # Capture table counters, without copying every historical per-record
        # revision. Publication still guards only the tables actually consumed.
        with ExitStack() as stack:
            with self._lock:
                self._store._snapshot_revisions.refresh()
                dependencies = {(table, '') for table in self._store._snapshot_revisions.tables
                                if table not in proposal_cache.TABLES}
                dependencies.update(proposal_cache.dependencies(self.conn))
                snapshot = stack.enter_context(self._store.read_snapshot(dependencies=dependencies))
            yield snapshot

    @snapshot_read
    def cleanup_head_rows(self, *, limit):
        if not self._store._recommendation_files:
            return []
        return [dict(row) for row in self.conn.execute(
            'SELECT surface,generation_id FROM recommendation_heads ORDER BY surface LIMIT ?', (limit,))]

    @staticmethod
    def _dtrack_row(r) -> dict:
        d = {k: r[k] for k in _DTRACK_FIELDS}
        d["audio"] = {c: r[c] for c in _DTRACK_AUDIO if r[c] is not None}
        return d

    @synchronized
    def record_discovery_impressions(self, kind, ids, view_id, now) -> None:
        """Count each visible Home Discover tile once per page visit, including across poll retries."""
        with self.conn:
            for value in dict.fromkeys(ids):
                identifier = provenance.subject(self.conn,kind,value)
                if identifier is not None:
                    provenance.impression(self.conn,kind,identifier,view_id,now)

    @snapshot_read
    def get_discovered_tracks(self) -> list:
        """Complete ranking inputs, detached and reusable against this reader's inputs."""
        def prepare():
            relation = read_owners(self.conn).relation('discovered_tracks')
            cursor = self.conn.execute(f'SELECT {_DTRACK_PROJECTION} FROM {relation}')
            rows = []
            while page := cursor.fetchmany(512):
                rows.extend(self._dtrack_row(row) for row in page)
            return rows
        return self._store.memoized_read('discovered-tracks', prepare)

    @snapshot_read
    def discovered_track_keys(self) -> set[str]:
        """Canonical pool membership without catalog metadata, audio or provenance."""
        relation = read_owners(self.conn).relation('discovery_memberships')
        return {row[0] for row in self.conn.execute(
            f'SELECT r.identity_key FROM {relation} d '
            'JOIN catalog_entries e ON e.id=d.entry_id AND e.recording_id=d.recording_id '
            'JOIN recordings r ON r.id=e.recording_id').fetchall()}

    @snapshot_read
    def identity_keys_for_video(self, video_id) -> list[str]:
        """Resolve discovery membership without decoding unrelated metadata or cached-only items."""
        return [r[0] for r in self.conn.execute(
            f"SELECT DISTINCT r.identity_key FROM {read_owners(self.conn).relation('discovery_memberships')} d "
            "JOIN catalog_entries e ON e.id=d.entry_id AND e.recording_id=d.recording_id "
            "JOIN recordings r ON r.id=e.recording_id WHERE e.video_id=?", (video_id,))]

    @snapshot_read
    def discovered_tracks_by_keys(self, keys, *, available_only=False, metadata_only=False) -> dict:
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        available = " AND available IS NOT 0" if available_only else ""
        if metadata_only:
            return {r['identity_key']: dict(r) for r in self.conn.execute(
                'SELECT identity_key,genre,year FROM ' + read_owners(self.conn).relation('discovered_tracks')
                + f' WHERE identity_key IN ({qs})' + available, keys)}
        return {r["identity_key"]: self._dtrack_row(r) for r in self.conn.execute(
            f"SELECT {_DTRACK_PROJECTION} FROM {read_owners(self.conn).relation('discovered_tracks')} "
            f"WHERE identity_key IN ({qs})" + available, keys)}

    @snapshot_read
    def candidate_tracks_by_keys(self, keys) -> dict:
        """Current playable candidate metadata without audio or offer history.

        Musical evidence has its own prepared profile owner. Selecting every
        discovery field here also evaluates unused metadata and provenance
        subqueries for each recording in a Vibe's overlapping rank pages.
        """
        keys = list(dict.fromkeys(keys))
        result = {}
        relation = read_owners(self.conn).relation('discovered_tracks')
        for offset in range(0, len(keys), 500):
            page = keys[offset:offset + 500]
            marks = ','.join('?' for _ in page)
            result.update({row['identity_key']: dict(row) for row in self.conn.execute(
                'SELECT identity_key,recording_id,video_id,title,artist,album,thumbnail,genre,year '
                f'FROM {relation} WHERE identity_key IN ({marks}) AND available IS NOT 0', page).fetchall()})
        return result

    @snapshot_read
    def discovery_track_rows(self) -> list:
        """Discovered tracks with play count (history), library-playlist membership count, offered
        count, saved metadata, and expiry (using found_at until shown). Newest first."""
        rows = self.conn.execute(
            "WITH plays AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key,COUNT(*) n FROM listening_days "
            "                            GROUP BY identity_key), "
            "     memberships AS MATERIALIZED (SELECT t.identity_key,COUNT(DISTINCT pt.playlist_id) n "
            "         FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id GROUP BY t.identity_key) "
            f"SELECT dt.*,COALESCE(p.n,0) plays,COALESCE(m.n,0) playlists FROM {read_owners(self.conn).relation('discovered_tracks')} dt "
            "LEFT JOIN plays p ON p.identity_key=dt.identity_key "
            "LEFT JOIN memberships m ON m.identity_key=dt.identity_key ORDER BY dt.found_at DESC").fetchall()
        out = []
        for r in rows:
            d = self._dtrack_row(r)
            d.update(plays=r["plays"], playlists=r["playlists"])
            out.append(d)
        return out

    @snapshot_read
    def discovery_album_rows(self) -> list:
        rows = self.conn.execute("SELECT * FROM "+read_owners(self.conn).relation('discovered_albums')+" ORDER BY found_at DESC").fetchall()
        return [{k: r[k] for k in ("browse_id", "artist", "title", "year", "thumbnail", "genre",
                                  "found_at", "first_shown", "offered_count", "legacy_offered_count")} for r in rows]

    @snapshot_read
    def discovery_artist_rows(self) -> list:
        rows = self.conn.execute(f"SELECT {self._artist_genre} AS genre,d.*,a.name display_artist FROM {self._artist_relation} d JOIN artists a ON a.id=d.artist_id ORDER BY found_at DESC").fetchall()
        return [{"artist":r['display_artist'],'artist_id':r['artist_id'], **{k: r[k] for k in ("score", "thumbnail", "genre",
                                      "found_at", "first_shown", "offered_count", "legacy_offered_count")}} for r in rows]

    @snapshot_read
    def expired_pool_rows(self, cutoff):
        """Query expired memberships, preserving recent sources and starter choices."""
        owners = read_owners(self.conn)
        sources, origins, artist_sources = (owners.relation(name) for name in
            ('discovery_sources', 'discovery_origins', 'discovery_artist_sources'))
        starter = (f"s.origin_id IN (SELECT id FROM {origins} WHERE kind='artist_start')"
                   if owners.typed else "s.source LIKE 'artist_start:%'")
        expired_albums = [row[0] for row in self.conn.execute(
            f"SELECT browse_id FROM {owners.relation('discovered_albums')} WHERE first_shown IS NOT NULL AND first_shown < ?",
            (cutoff,))]
        expired_artists = [row[0] for row in self.conn.execute(
            f"SELECT d.artist_id FROM {owners.relation('discovered_artists')} d WHERE first_shown IS NOT NULL AND first_shown < ? "
            f'AND NOT EXISTS(SELECT 1 FROM {artist_sources} s WHERE s.artist_id=d.artist_id AND '+starter+')', (cutoff,))]
        retained_starter = (
            f'JOIN {origins} o ON o.id=s.origin_id '
            f'JOIN {artist_sources} a ON a.artist_id=o.artist_id '
            f'JOIN {origins} starter ON starter.id=a.origin_id '
            "WHERE s.recording_id=d.recording_id AND o.kind='artist' "
            "AND starter.kind='artist_start' AND starter.artist_id=o.artist_id" if owners.typed else
            "JOIN artist_provider_ids p ON p.provider='youtube' AND s.source_token='artist:'||p.provider_id "
            f'JOIN {artist_sources} a ON a.artist_id=p.artist_id '
            "WHERE s.recording_id=d.recording_id AND a.source='artist_start:'||p.provider_id")
        tracks = [row[0] for row in self.conn.execute(
            f"SELECT d.identity_key FROM {owners.relation('discovered_tracks')} d "
            "WHERE COALESCE(first_shown, found_at) < ? "
            f"AND NOT EXISTS(SELECT 1 FROM {sources} s WHERE "
            "s.recording_id=d.recording_id AND s.last_found_at>=?) "
            f"AND NOT EXISTS(SELECT 1 FROM {sources} s " + retained_starter + ')', (cutoff, cutoff))]
        return {'albums': expired_albums, 'artists': expired_artists,
                'artist_targets': owners.installed_artist_ids(expired_artists), 'tracks': tracks}

    @snapshot_read
    def cleanup_track_details(self, keys):
        keys = list(keys)
        sources, owned = set(), set()
        native = read_owners(self.conn).native
        for offset in range(0, len(keys), 500):
            page = keys[offset:offset + 500]
            marks = ','.join('?' for _ in page)
            sources.update(row[0] for row in self.conn.execute(
                ('SELECT t.source_token FROM discovery_sources s '
                 'JOIN discovery_origin_tokens t ON t.id=s.origin_id ' if native else
                 'SELECT s.source_token FROM discovery_sources s ') +
                'JOIN recordings r ON r.id=s.recording_id WHERE r.identity_key IN (' + marks + ') AND ' +
                ("t.kind" if native else 's.source_type') + " IN ('album','radio','genre','artist')", page))
            owned.update(row[0] for row in self.conn.execute(
                f'SELECT identity_key FROM tracks WHERE identity_key IN ({marks})', page))
        return {'sources': sources, 'unowned': set(keys) - owned}

    @synchronized
    def remember_source_tokens(self, tokens):
        from yt_playlist.core import discovery_checkpoints
        with self.conn.atomic():
            for token in tokens:
                discovery_checkpoints.remember(self.conn, token)

    @synchronized
    def delete_pool_entries(self, albums, artists, tracks):
        with self.conn.atomic():
            self.conn.executemany('DELETE FROM main.discovered_albums WHERE browse_id=?', [(bid,) for bid in albums])
            self.conn.executemany('DELETE FROM main.discovered_artists WHERE artist_id=?', [(aid,) for aid in artists])
            self.conn.executemany('DELETE FROM discovery_memberships WHERE recording_id IN '
                                  '(SELECT id FROM recordings WHERE identity_key=?)', [(key,) for key in tracks])

    @snapshot_read
    def prunable_album_rows(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT d.browse_id,p.album_id FROM discovered_albums d LEFT JOIN album_provider_ids p "
            "ON p.provider='youtube' AND p.provider_id=d.browse_id")]

    @snapshot_read
    def prunable_artist_ids(self):
        starter = ("s.origin_id IN (SELECT id FROM discovery_origins WHERE kind='artist_start')"
                   if provenance.available(self.conn) else "s.source LIKE 'artist_start:%'")
        return [row[0] for row in self.conn.execute(
            "SELECT artist_id FROM discovered_artists d WHERE NOT EXISTS("
            'SELECT 1 FROM discovery_artist_sources s WHERE s.artist_id=d.artist_id AND '+starter+')')]
