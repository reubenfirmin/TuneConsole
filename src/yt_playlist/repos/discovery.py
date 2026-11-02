"""DiscoveryRepo: the background outward-discovery state: which artists we've scanned (and when),
and the accumulating pools of new albums / new artists we've found, with last-shown bookkeeping.

Tables are created by numbered migrations. The interest signal (which artists are worth
scanning) is computed live from your library: play counts, playlist participation, saved albums.
"""
import json
from uuid import uuid4
from contextlib import contextmanager, ExitStack
from yt_playlist.core import (album_aliases, album_memberships, collection_recovery,
                              discovery_provenance as provenance, policy_documents)

from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.core import catalog, artists, albums, genre_classifications, metadata, metadata_evidence, metadata_hints, metadata_runtime
from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.core.db_diagnostics import database_operation
from yt_playlist.core.discovery_sources import retain as retain_source
from yt_playlist.repos import discovery_cache, proposal_cache
from yt_playlist.core.preparation.discovery_publication import read_owners

# Audio/content columns persisted per discovered track (mirrors the library `tracks` audio set).
_DTRACK_AUDIO = ("bpm", "energy", "danceability", "mood_happy", "mood_sad", "mood_relaxed",
                 "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
                 "music_key", "music_scale")

_GENERATED_PL = "(SELECT playlist_scope_id FROM quarantined_playlists)"


class DiscoveryRepo(Repo):

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

    @synchronized
    def select_starter_artist(self, name, browse_id, thumbnail, selected, now):
        """Save the real taste input; no onboarding-complete or roadmap flags."""
        from yt_playlist.core.discovery_sources import retain_artist
        with self.conn:
            aid = artists.ensure(self.conn, name, provider='youtube', provider_id=browse_id)
            typed = provenance.available(self.conn)
            starter = ("origin_id IN (SELECT id FROM discovery_origins WHERE kind='artist_start')" if typed else
                       "source LIKE 'artist_start:%'")
            if selected:
                count = self.conn.execute("SELECT COUNT(DISTINCT s.artist_id) FROM discovery_artist_sources s "
                    'WHERE '+starter+' AND s.artist_id<>?', (aid,)).fetchone()[0]
                if count >= 12:
                    raise ValueError('Choose up to 12 artists to start.')
                self.conn.execute('DELETE FROM main.discovery_artist_sources WHERE artist_id=? AND '+starter, (aid,))
                retain_artist(self.conn, aid, 'artist_start:' + browse_id, 3.)
                self.conn.execute(
                    'INSERT INTO discovered_artists(artist_id,score,thumbnail,found_at'+('' if typed else ',artist')+') '
                    'VALUES (?,3,?,?'+('' if typed else ',?')+') ON CONFLICT(artist_id) DO UPDATE SET '
                    "thumbnail=COALESCE(NULLIF(excluded.thumbnail,''),discovered_artists.thumbnail)",
                    (aid, thumbnail, now) + (() if typed else (f'@artist:{aid}',)))
                provenance.refresh_counts(self.conn,'artist',aid)
            else:
                self.conn.execute('DELETE FROM main.discovery_artist_sources WHERE artist_id=? AND '+starter, (aid,))
                provenance.invalidate_explanation(self.conn,aid)
        return self.starter_artists()

    @snapshot_read
    def starter_tracks(self):
        """Only retained, available songs from the currently selected provider identities."""
        length_clause = self._store.rec_eligibility.track_length_clause('c.duration_s')
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
                'ORDER BY p.artist_id,s.found_at,c.recording_id').fetchall()
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
                'ORDER BY p.artist_id,s.first_found_at,c.recording_id').fetchall()
        groups = {}
        for row in rows:
            groups.setdefault(row['seed'], []).append(dict(row))
        result, seen = [], set()
        for index in range(4):
            for group in groups.values():
                if index < len(group) and group[index]['recording_id'] not in seen:
                    result.append(group[index])
                    seen.add(group[index]['recording_id'])
        return result[:24]

    # --- interest: every artist you engage with, ranked by engagement intensity. Weights ramp with how
    # deliberate the act is: a play is passive (1), filing into a playlist is active curation (2), saving
    # a whole album is the strongest endorsement (3). Tune the multipliers in the SELECT below. ---
    @snapshot_read
    def interested_artists(self, limit=None) -> list:
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
            "       COALESCE(plays.n,0)*1.0 + COALESCE(pls.n,0)*2.0 + COALESCE(sv.n,0)*3.0 + COALESCE(seeds.n,0) score "
            "FROM arts JOIN artists a ON a.id=arts.a LEFT JOIN artist_provider_ids ap "
            "ON ap.artist_id=a.id AND ap.provider='youtube' "
            "LEFT JOIN plays ON plays.a=arts.a LEFT JOIN pls ON pls.a=arts.a "
            "LEFT JOIN sv ON sv.a=arts.a LEFT JOIN seeds ON seeds.a=arts.a ORDER BY score DESC"
            + (" LIMIT ?" if limit is not None else ""),
            (limit,) if limit is not None else ()).fetchall()
        return [dict(r) for r in rows]

    @snapshot_read
    def artists_due_for_scan(self, now, ttl_days=5, budget=25, artist_limit=None, *, with_ids=False) -> list:
        """Interested artists not scanned within ttl_days (never-scanned first, then oldest), capped
        at budget, exactly which artists the next worker pass should hit the network for. `artist_limit`
        (#52) restricts the universe to your top-N most-engaged artists, so discovery stays focused.

        ttl_days=5 re-scans an artist about weekly (their catalogue changes slowly); budget=25 rate-
        limits each pass so we don't burst the Last.fm/MusicBrainz APIs."""
        cutoff = now - ttl_days * 86400
        scanned = {r["artist_id"]: r["scanned_at"] for r in
                   self.conn.execute("SELECT artist_id, scanned_at FROM "+read_owners(self.conn).relation('artist_scans'))}
        due = [a for a in self.interested_artists(limit=artist_limit)
               if a["artist_id"] not in scanned or (scanned[a["artist_id"]] or 0.0) < cutoff]
        due.sort(key=lambda a: (scanned.get(a["artist_id"]) or float("-inf"), -a["score"]))
        return due[:budget] if with_ids else [a['artist'] for a in due[:budget]]

    @snapshot_read
    def discovered_albums_for_artist(self, artist) -> dict:
        """{browse_id: offered_count} for one artist's pooled albums (#52 rotation input)."""
        aid = artists.lookup(self.conn,artist)
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

    @synchronized
    def prune_orphan_discovered_tracks(self, held_keys=()) -> int:
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
        held = set(held_keys)
        gone = [(r["identity_key"],) for r in rows if r["identity_key"] not in held]
        self.conn.executemany("DELETE FROM discovery_memberships WHERE recording_id IN "
                              "(SELECT id FROM recordings WHERE identity_key=?)", gone)
        self.conn.commit()
        return len(gone)

    @synchronized
    def mark_scanned(self, artist, now) -> None:
        aid = artists.state_id(self.conn,artist)
        typed = provenance.available(self.conn)
        self.conn.execute('INSERT INTO artist_scans(artist_id,scanned_at'+('' if typed else ',artist')+') VALUES (?,?'+
                          ('' if typed else ',?')+') ON CONFLICT(artist_id) DO UPDATE SET scanned_at=excluded.scanned_at',
                          (aid,now) + (() if typed else (f'@artist:{aid}',)))
        self.conn.commit()

    # --- the accumulating pools ---
    @synchronized
    def upsert_discovered_album(self, browse_id, artist, title, year, thumbnail, now, genre=None, *, artist_id=None) -> None:
        """Add an album to the pool (first-seen found_at is preserved on re-discovery). `genre` is the
        candidate genre for the #18 facet overlay; a later re-discovery with a genre fills it in."""
        aid = artists.state_id(self.conn,artist_id if artist_id is not None else artist)
        with self.conn:
            album_id = albums.observe(self.conn,browse_id,title=title,artist=artist,year=year,
                                      thumbnail=thumbnail,genre=genre,artist_id=aid,rank=1)
            self.conn.execute('INSERT INTO album_discovery_memberships(album_id,found_at) VALUES (?,?) ON CONFLICT(album_id) DO NOTHING', (album_id,now))
            provenance.refresh_counts(self.conn,'album',album_id)

    @synchronized
    def upsert_discovered_artist(self, artist, score, because, fits, thumbnail, now, genre=None, *, artist_id=None,
                                 source='discovery') -> None:
        aid = artists.state_id(self.conn,artist_id if artist_id is not None else artist)
        from yt_playlist.core.discovery_sources import retain_artist
        with self.conn:
            retain_artist(self.conn,aid,source,score)
            typed = provenance.available(self.conn)
            self.conn.execute(
                'INSERT INTO discovered_artists(artist_id,score,thumbnail,found_at'+('' if typed else ',artist')+') '
                'VALUES (?,?,?,?'+('' if typed else ',?')+') ON CONFLICT(artist_id) DO UPDATE SET score=excluded.score, '
                "thumbnail=COALESCE(NULLIF(excluded.thumbnail,''), discovered_artists.thumbnail)",
                (aid,score,thumbnail,now) + (() if typed else (f'@artist:{aid}',)))
            provenance.explain(self.conn,aid,because,fits)
            provenance.refresh_counts(self.conn,'artist',aid)
            if genre is not None:
                genre_classifications.write(self.conn, 'discovered_artists', aid, genre)

    @lookup_read
    def artist_images_due(self, artist_ids, now):
        """Only the bounded on-screen cohort, with a day between failed lookups."""
        ids = list(dict.fromkeys(artist_ids))[:40]
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
    def record_artist_image(self, artist_id, thumbnail, now, *, retry_after=86400):
        """An image lookup cannot overwrite rankings, provenance or exposure counts."""
        with self.conn:
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_artist_image_state'").fetchone():
                self.conn.execute('INSERT INTO discovery_artist_image_state(artist_id,retry_at) VALUES (?,?) '
                    'ON CONFLICT(artist_id) DO UPDATE SET retry_at=excluded.retry_at',(artist_id,now+retry_after))
            else:
                self.conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (f'discovery:artist-image:{artist_id}', str(now + retry_after)))
            if thumbnail:
                self.conn.execute("UPDATE discovered_artists SET thumbnail=? WHERE artist_id=? "
                                  "AND COALESCE(thumbnail,'')=''", (thumbnail, artist_id))

    @synchronized
    def retain_discovered_artist(self, artist, score):
        """Retain independent interest without overwriting an existing suggestion's metadata."""
        from yt_playlist.core.discovery_sources import retain_artist
        aid = artists.lookup(self.conn,artist,exact=True)
        if aid is not None and self.conn.execute(
                'SELECT 1 FROM discovered_artists WHERE artist_id=?', (aid,)).fetchone():
            with self.conn:
                if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_artist_sources'").fetchone():
                    return
                # When an independent import adopts a Spotify-only suggestion,
                # its score must also survive without retaining Spotify's count.
                independent = ("origin_id IN (SELECT id FROM discovery_origins WHERE kind NOT IN ('spotify_export','spotify_model'))"
                               if provenance.available(self.conn) else "source NOT LIKE 'spotify_export:%' AND source<>'spotify_model'")
                self.conn.execute('UPDATE discovered_artists SET score=? WHERE artist_id=? '
                    'AND NOT EXISTS (SELECT 1 FROM discovery_artist_sources WHERE artist_id=? '
                    'AND '+independent+')', (score,aid,aid))
                retain_artist(self.conn,aid,'discovery',score)

    @synchronized
    def seed_spotify_artists(self, account_id, counts, now, *, min_plays=3, import_key=None):
        """Retain import provenance even when an artist was independently discovered."""
        from yt_playlist.core.discovery_sources import retain_artist
        seeded = 0
        typed = provenance.available(self.conn)
        with self.conn:
            for name, plays in counts.items():
                if plays < min_plays:
                    continue
                aid = artists.state_id(self.conn,name)
                retain_artist(self.conn,aid,f'spotify_export:{account_id}',float(plays),account_id,import_key=import_key)
                seeded += self.conn.execute(
                    'INSERT INTO discovered_artists(artist_id,score,found_at'+('' if typed else ',artist')+') '
                    'VALUES (?,?,?'+('' if typed else ',?')+') ON CONFLICT(artist_id) DO NOTHING',
                    (aid,float(plays),now) + (() if typed else (f'@artist:{aid}',))).rowcount
                independent = ("origin_id IN (SELECT id FROM discovery_origins WHERE kind<>'spotify_export')"
                               if typed else "source NOT LIKE 'spotify_export:%'")
                self.conn.execute('UPDATE discovered_artists SET score=('
                    'SELECT MAX(score) FROM discovery_artist_sources WHERE artist_id=?) WHERE artist_id=? '
                    'AND NOT EXISTS (SELECT 1 FROM discovery_artist_sources WHERE artist_id=? '
                    'AND '+independent+')', (aid,aid,aid))
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
    def get_discovered_artists(self) -> list:
        with policy_documents.retained_reader(collection_recovery.directory(self.conn)):
            return [{"artist": r["display_artist"], "artist_id":r['artist_id'], "score": r["score"], **provenance.explanation(self.conn,r), "thumbnail": r["thumbnail"],
                     "found_at": r["found_at"], "last_shown": r["last_shown"], "genre": r["genre"],
                     "first_shown": r["first_shown"], "offered_count": r["offered_count"]}
                    for r in self.conn.execute(f"SELECT {self._artist_genre} AS genre,d.*,a.name display_artist FROM {self._artist_relation} d JOIN artists a ON a.id=d.artist_id")]

    @property
    def _artist_genre(self):
        return genre_classifications.read_expression(self.conn, 'd', 'discovered_artists')

    # --- #13 Phase 2: out-of-corpus candidate tracks (for the Clusters "reach for new music" mode) ---
    @synchronized
    def upsert_discovered_track(self, identity_key, video_id, title, artist, album, thumbnail,
                                genre, year, source_browse_id, now, audio=None, *, credits=None,
                                duration_s=None) -> str:
        with self.conn:
            entry_id = catalog.upsert(self.conn, video_id, title, artist, album,
                                      thumbnail=thumbnail, created_at=now, library=False,
                                      preferred_key=identity_key,credits=credits,duration_s=duration_s)
            row = self.conn.execute("SELECT recording_id,identity_key FROM catalog_tracks WHERE id=?", (entry_id,)).fetchone()
            rid = row["recording_id"]
            catalog.add_alias(self.conn, identity_key, rid, "discovery")
            self.conn.execute(
                "INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) "
                "ON CONFLICT(recording_id) DO NOTHING", (rid,entry_id,now))
            provenance.refresh_counts(self.conn,'track',rid)
            if source_browse_id:
                retain_source(self.conn, rid, source_browse_id, video_id or None, now)
                if self.conn.execute("SELECT 1 FROM discovered_albums WHERE browse_id=?",
                                     (album_aliases.primary_id(self.conn, source_browse_id),)).fetchone():
                    albums.link(self.conn,entry_id,source_browse_id,source='discovery')
            metadata.fill(self.conn,entry_id,{'genre':genre,'mb_year':year,
                **{field:value for field,value in (audio or {}).items() if field in _DTRACK_AUDIO}},
                'discovery_import',now=now)
            for field,value in {'genre':genre,'mb_year':year,**(audio or {})}.items():
                if value is not None and field in {'genre','mb_year',*_DTRACK_AUDIO}:
                    if metadata_evidence.available(self.conn):
                        metadata_hints.put(self.conn, entry_id, field, source_browse_id or 'discovery', value, now=now)
                    elif not metadata_runtime.active(self.conn):
                        self.conn.execute(
                            'INSERT OR IGNORE INTO catalog_field_hints(entry_id,field,source,value_json) VALUES (?,?,?,?)',
                            (entry_id,field,source_browse_id or 'discovery',json.dumps(value)))
            return self.conn.execute("SELECT identity_key FROM recordings WHERE id=?", (rid,)).fetchone()[0]

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

    def remove_discovery_source(self, key, source_token, held_keys=()) -> bool:
        """Remove one contribution; only the last source can remove a candidate."""
        with (database_operation('DiscoveryRepo.remove_discovery_source'), work_budget(20),
              self._cleanup_snapshot() as snapshot):
            reader = snapshot.store
            owners = read_owners(reader.conn)
            source_column = 'origin_id' if owners.typed else 'source_token'
            origin_id = owners.origin(source_token)
            sources = owners.relation('discovery_sources')
            held = set(held_keys) | reader.rec_eligibility.generated_track_keys()
            row = reader.conn.execute(
                f"SELECT d.recording_id FROM {owners.relation('discovery_memberships')} d JOIN recordings r ON r.id=d.recording_id "
                f"WHERE r.identity_key=? AND EXISTS(SELECT 1 FROM {sources} s "
                f'WHERE s.recording_id=d.recording_id AND s.{source_column}=?) '
                f"AND NOT EXISTS(SELECT 1 FROM {sources} s "
                f'WHERE s.recording_id=d.recording_id AND s.{source_column} IS NOT ?)', (key, origin_id,origin_id)).fetchone()
            recording = reader.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
            installed_sources = owners.installed_source_tokens(recording[0], origin_id) if recording else set()
            installed_column = 'origin_id' if owners.native else 'source_token'
            remove = row is not None and key not in held
            rows = proposal_cache.logical_rows(reader.conn, store=reader) if remove else []
            snapshot.release_reader()
            prepared = discovery_cache.prepare(rows, {key}) if remove else []
            prepared = discovery_cache.stage(self._store.rec_proposals, prepared)

            def publish():
                # ReadSnapshot owns the transaction and already holds this reentrant
                # lock. Keep shared connection ownership explicit at the SQL boundary.
                with self._lock:
                    self.conn.executemany(
                        f'DELETE FROM main.discovery_sources WHERE {installed_column}=? AND recording_id='
                        "(SELECT id FROM recordings WHERE identity_key=?)", [(token, key) for token in installed_sources])
                    if remove:
                        self.conn.execute("DELETE FROM discovery_memberships WHERE recording_id=?", (row[0],))
                        discovery_cache.publish(self.conn, prepared)

            if not snapshot.publish(publish):
                raise WorkBudgetExceeded('Discovery source removal inputs changed; retry cleanup')
            return remove

    @contextmanager
    def _cleanup_snapshot(self):
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
            if self._store._recommendation_files:
                # Guard committed publication replacements without reading pool payloads
                # or invalidating cleanup for an unpublished successor generation.
                heads = snapshot.store.conn.execute(
                    'SELECT surface,generation_id FROM recommendation_heads ORDER BY surface LIMIT 129').fetchall()
                if len(heads) > 128:
                    raise WorkBudgetExceeded('Recommendation head inventory exceeds its read budget')
            yield snapshot

    def _prune_cached_tracks(self, gone_keys):
        """Prune caches independently; deletion callers prepare in their own snapshot."""
        with (database_operation('DiscoveryRepo._prune_cached_tracks'), work_budget(20),
              self._cleanup_snapshot() as snapshot):
            reader = snapshot.store
            rows = proposal_cache.logical_rows(reader.conn, store=reader)
            reader.conn.execute('SELECT 1 FROM library_memberships LIMIT 1').fetchone()
            reader.conn.execute('SELECT 1 FROM recordings LIMIT 1').fetchone()
            snapshot.release_reader()
            prepared = discovery_cache.prepare(rows, set(gone_keys))
            prepared = discovery_cache.stage(self._store.rec_proposals, prepared)
            return snapshot.publish(lambda: discovery_cache.publish(self.conn, prepared))

    @staticmethod
    def _dtrack_row(r) -> dict:
        d = {k: r[k] for k in ("id", "recording_id", "identity_key", "video_id", "title", "artist", "album", "thumbnail",
                               "genre", "year", "source_browse_id", "found_at",
                               "first_shown", "last_shown", "offered_count", "last_enriched")}
        d["audio"] = {c: r[c] for c in _DTRACK_AUDIO if r[c] is not None}
        return d

    @synchronized
    def mark_offered(self, kind, ids, now) -> None:
        """Record a distinct surfacing for an existing candidate."""
        self.record_discovery_impressions(kind,ids,'offer:'+uuid4().hex,now)

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
        return [self._dtrack_row(r) for r in self.conn.execute("SELECT * FROM "+read_owners(self.conn).relation('discovered_tracks'))]

    @snapshot_read
    def identity_keys_for_video(self, video_id) -> list[str]:
        """Resolve discovery membership without decoding unrelated metadata or cached-only items."""
        return [r[0] for r in self.conn.execute(
            f"SELECT DISTINCT r.identity_key FROM {read_owners(self.conn).relation('discovery_memberships')} d "
            "JOIN catalog_entries e ON e.id=d.entry_id AND e.recording_id=d.recording_id "
            "JOIN recordings r ON r.id=e.recording_id WHERE e.video_id=?", (video_id,))]

    @snapshot_read
    def discovered_tracks_by_keys(self, keys, *, available_only=False) -> dict:
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        available = " AND available IS NOT 0" if available_only else ""
        return {r["identity_key"]: self._dtrack_row(r) for r in self.conn.execute(
            f"SELECT * FROM {read_owners(self.conn).relation('discovered_tracks')} WHERE identity_key IN ({qs})" + available, keys)}

    # --- Discovery persistence used by the shared enrichment worker. ----------------------------
    @synchronized
    def set_discovered_enrichment(self, identity_key, genre, year) -> None:
        """Select supplied genre/year; a None argument leaves the existing value."""
        row = self.conn.execute('SELECT id FROM discovered_tracks WHERE identity_key=?',(identity_key,)).fetchone()
        if row is not None:
            with self.conn:
                for field,value in (('genre',genre),('mb_year',year)):
                    if value is not None:
                        metadata.select(self.conn,row['id'],field,value,'discovery_enrichment')

    @synchronized
    def set_discovered_audio(self, identity_key, **audio) -> None:
        """Write the audio columns this table holds; ignore any extra provider fields (e.g. popularity,
        gain, label) that have no discovered_tracks column."""
        cols = [c for c in _DTRACK_AUDIO if c in audio]
        if not cols:
            return
        row = self.conn.execute('SELECT id FROM discovered_tracks WHERE identity_key=?',(identity_key,)).fetchone()
        if row is not None:
            with self.conn:
                for field in cols:
                    metadata.select(self.conn,row['id'],field,audio[field],'discovery_audio')

    @synchronized
    def mark_discovered_enriched(self, identity_keys, now) -> None:
        """Stamp last_enriched so a probed row leaves the queue (probe-once for v1)."""
        keys = list(identity_keys)
        if not keys:
            return
        qs = ",".join("?" * len(keys))
        if not self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='catalog_entries'").fetchone():
            # This is the physical predecessor table, used before its one-time upgrade.
            self.conn.execute(f"UPDATE discovered_tracks SET last_enriched=? WHERE identity_key IN ({qs})",[now]+keys)
        else:
            self.conn.execute(f"UPDATE recordings SET first_enriched_at=COALESCE(first_enriched_at,?),last_enriched_at=? "
                f"WHERE identity_key IN ({qs}) AND id IN (SELECT recording_id FROM discovery_memberships)", [now,now]+keys)
        self.conn.commit()

    @synchronized
    def prune_discovered_tracks(self, library_keys, held_keys=()) -> None:
        """Drop candidate tracks you've since acquired (now in the library), EXCEPT keys in held_keys.
        #52: a track that is only in a live generated playlist is held in the pool until that playlist
        is GC'd or promoted, so its synced copy must not prune it as 'acquired' in the meantime."""
        held = set(held_keys or ())
        rows = self.conn.execute("SELECT identity_key FROM discovered_tracks").fetchall()
        gone = [r["identity_key"] for r in rows
                if r["identity_key"] in library_keys and r["identity_key"] not in held]
        if gone:
            self.conn.executemany("DELETE FROM discovery_memberships WHERE recording_id IN "
                                  "(SELECT id FROM recordings WHERE identity_key=?)",
                                  [(k,) for k in gone])
            self.conn.commit()

    # --- #53 Tools viewer: per-pool rows with engagement stats and the projected GC date ---
    @staticmethod
    def _gc_at(first_shown, gc_days):
        return (first_shown + gc_days * 86400) if first_shown is not None else None

    @snapshot_read
    def discovery_track_view(self, now, gc_days) -> list:
        """Discovered tracks with play count (history), library-playlist membership count, offered
        count, saved metadata, and expiry (using found_at until shown). Newest first."""
        rows = self.conn.execute(
            "WITH plays AS MATERIALIZED (SELECT identity_key,COUNT(*) n FROM listening_days "
            "                            GROUP BY identity_key), "
            "     memberships AS MATERIALIZED (SELECT t.identity_key,COUNT(DISTINCT pt.playlist_id) n "
            "         FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id GROUP BY t.identity_key) "
            f"SELECT dt.*,COALESCE(p.n,0) plays,COALESCE(m.n,0) playlists FROM {read_owners(self.conn).relation('discovered_tracks')} dt "
            "LEFT JOIN plays p ON p.identity_key=dt.identity_key "
            "LEFT JOIN memberships m ON m.identity_key=dt.identity_key ORDER BY dt.found_at DESC").fetchall()
        out = []
        for r in rows:
            d = self._dtrack_row(r)
            d.update(plays=r["plays"], playlists=r["playlists"],
                     gc_at=self._gc_at(r["first_shown"] if r["first_shown"] is not None
                                       else r["found_at"], gc_days))
            out.append(d)
        return out

    @snapshot_read
    def discovery_album_view(self, now, gc_days) -> list:
        rows = self.conn.execute("SELECT * FROM "+read_owners(self.conn).relation('discovered_albums')+" ORDER BY found_at DESC").fetchall()
        return [{**{k: r[k] for k in ("browse_id", "artist", "title", "year", "thumbnail", "genre",
                                      "found_at", "first_shown", "offered_count", "legacy_offered_count")},
                 "gc_at": self._gc_at(r["first_shown"], gc_days)} for r in rows]

    @snapshot_read
    def discovery_artist_view(self, now, gc_days) -> list:
        rows = self.conn.execute(f"SELECT {self._artist_genre} AS genre,d.*,a.name display_artist FROM {self._artist_relation} d JOIN artists a ON a.id=d.artist_id ORDER BY found_at DESC").fetchall()
        return [{"artist":r['display_artist'],'artist_id':r['artist_id'], **{k: r[k] for k in ("score", "thumbnail", "genre",
                                      "found_at", "first_shown", "offered_count", "legacy_offered_count")},
                 "gc_at": self._gc_at(r["first_shown"], gc_days)} for r in rows]

    def gc_discovery_pool(self, now, gc_days, held_track_keys) -> dict:
        """Expire old suggestions. Unshown tracks age from discovery instead of living forever.

        Albums/artists retain their first-view lifetime. Explicit starter choices
        and their songs survive; tracks in live generated playlists are also held.
        Purge removed tracks from materialized recommendations in the same transaction.
        """
        with (database_operation('DiscoveryRepo.gc_discovery_pool'), work_budget(20),
              self._cleanup_snapshot() as snapshot):
            reader = snapshot.store
            cutoff = now - gc_days * 86400
            owners = read_owners(reader.conn)
            sources, origins, artist_sources = (owners.relation(name) for name in
                ('discovery_sources', 'discovery_origins', 'discovery_artist_sources'))
            starter = (f"s.origin_id IN (SELECT id FROM {origins} WHERE kind='artist_start')"
                       if owners.typed else "s.source LIKE 'artist_start:%'")
            expired_albums = [row[0] for row in reader.conn.execute(
                f"SELECT browse_id FROM {owners.relation('discovered_albums')} WHERE first_shown IS NOT NULL AND first_shown < ?",
                (cutoff,))]
            expired_artists = [row[0] for row in reader.conn.execute(
                f"SELECT d.artist_id FROM {owners.relation('discovered_artists')} d WHERE first_shown IS NOT NULL AND first_shown < ? "
                f'AND NOT EXISTS(SELECT 1 FROM {artist_sources} s WHERE s.artist_id=d.artist_id AND '+starter+')', (cutoff,))]
            installed_artists = owners.installed_artist_ids(expired_artists)
            out = {'albums': len(expired_albums), 'artists': len(expired_artists)}
            held = set(held_track_keys or ()) | reader.rec_eligibility.generated_track_keys()
            retained_starter = (
                f'JOIN {origins} o ON o.id=s.origin_id '
                f'JOIN {artist_sources} a ON a.artist_id=o.artist_id '
                f'JOIN {origins} starter ON starter.id=a.origin_id '
                "WHERE s.recording_id=d.recording_id AND o.kind='artist' "
                "AND starter.kind='artist_start' AND starter.artist_id=o.artist_id" if owners.typed else
                "JOIN artist_provider_ids p ON p.provider='youtube' AND s.source_token='artist:'||p.provider_id "
                f'JOIN {artist_sources} a ON a.artist_id=p.artist_id '
                "WHERE s.recording_id=d.recording_id AND a.source='artist_start:'||p.provider_id")
            rows = reader.conn.execute(
                f"SELECT d.identity_key,d.recording_id FROM {owners.relation('discovered_tracks')} d "
                "WHERE COALESCE(first_shown, found_at) < ? "
                f"AND NOT EXISTS(SELECT 1 FROM {sources} s WHERE "
                "s.recording_id=d.recording_id AND s.last_found_at>=?) "
                f"AND NOT EXISTS(SELECT 1 FROM {sources} s "
                +retained_starter+')',
                (cutoff, cutoff)).fetchall()
            gone = {r['identity_key'] for r in rows if r['identity_key'] not in held}
            source_tokens = {s[0] for r in rows if r['identity_key'] in gone
                             for s in reader.conn.execute(
                                 ('SELECT t.source_token FROM discovery_sources s '
                                  'JOIN discovery_origin_tokens t ON t.id=s.origin_id '
                                  "WHERE s.recording_id=? AND t.kind IN ('album','radio','genre','artist')" if owners.native else
                                  "SELECT source_token FROM discovery_sources WHERE recording_id=? AND source_type IN ('album','radio','genre','artist')"),
                                 (r['recording_id'],))}
            attempts = [('enrich_attempts:' + key,) for key in gone if not reader.conn.execute(
                'SELECT 1 FROM tracks WHERE identity_key=?', (key,)).fetchone()]
            cached = proposal_cache.logical_rows(reader.conn, store=reader) if gone else []
            snapshot.release_reader()
            prepared = discovery_cache.prepare(cached, gone) if gone else []
            prepared = discovery_cache.stage(self._store.rec_proposals, prepared)

            def publish():
                # Only prepared writes run here; the snapshot still owns the
                # revision check, transaction, commit and subsequent checkpoint.
                with self._lock:
                    self.conn.executemany('DELETE FROM main.discovered_albums WHERE browse_id=?',
                                          [(bid,) for bid in expired_albums])
                    self.conn.executemany('DELETE FROM main.discovered_artists WHERE artist_id=?',
                                          [(aid,) for aid in installed_artists])
                    if gone:
                        # Fetch history prevents the next scan from refilling expired albums.
                        from yt_playlist.core import discovery_checkpoints
                        for token in source_tokens:
                            discovery_checkpoints.remember(self.conn,token)
                        self.conn.executemany('DELETE FROM discovery_memberships WHERE recording_id IN '
                                              '(SELECT id FROM recordings WHERE identity_key=?)',
                                              [(key,) for key in gone])
                        self.conn.executemany('DELETE FROM settings WHERE key=?', attempts)
                        discovery_cache.publish(self.conn, prepared)

            if not snapshot.publish(publish):
                raise WorkBudgetExceeded('Discovery pool cleanup inputs changed; retry cleanup')
            return {**out, 'tracks': len(gone)}

    def mark_shown(self, kind, ids, now) -> None:
        """Back-compat alias for mark_offered: stamp a surfacing (last_shown + first_shown + offered_count).
        kind='album' (browse_ids) | 'artist' (names)."""
        self.mark_offered(kind, ids, now)

    @synchronized
    def prune_discovered(self, owned_albums, saved_browse_ids, owned_artists) -> None:
        """Drop pool entries you've since acquired: albums you now own/saved, artists now in library."""
        for r in self.conn.execute("SELECT browse_id, title FROM discovered_albums").fetchall():
            if r["browse_id"] in saved_browse_ids or albums.lookup(self.conn,r['browse_id']) in owned_albums:
                self.conn.execute("DELETE FROM discovered_albums WHERE browse_id=?", (r["browse_id"],))
        if owned_artists:
            starter = ("s.origin_id IN (SELECT id FROM discovery_origins WHERE kind='artist_start')"
                       if provenance.available(self.conn) else "s.source LIKE 'artist_start:%'")
            rows = self.conn.execute("SELECT artist_id FROM discovered_artists d WHERE NOT EXISTS("
                'SELECT 1 FROM discovery_artist_sources s WHERE s.artist_id=d.artist_id AND '+starter+')').fetchall()
            public = artists.public_keys(self.conn, [r['artist_id'] for r in rows])
            for r in rows:
                if artists.token(r['artist_id']) in owned_artists or public[r['artist_id']] in owned_artists:
                    self.conn.execute("DELETE FROM discovered_artists WHERE artist_id=?", (r["artist_id"],))
        self.conn.commit()
