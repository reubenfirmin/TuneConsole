"""CollectionRepo: saved-album CRUD plus the aggregate playlist and saved-album
library views that power the collection pages.
"""
import json
import time
from yt_playlist.core import artists as artist_catalog, albums as album_catalog

from yt_playlist.repos.base import Repo, synchronized


class CollectionRepo(Repo):
    @synchronized
    def cache_album_tracklist(self, browse_id, title, artist, thumbnail, keys, *, video_ids=None, entries=None, credits=None, year=None, album_type=None):
        """Only full album fetches may write this denominator; playlist sync cannot."""
        with self.conn:
            album_catalog.cache_full(self.conn,browse_id,title,artist,thumbnail,keys,entries=entries,credits=credits,year=year,album_type=album_type)
            self.conn.execute("DELETE FROM album_share_snapshots WHERE browse_id=?", (browse_id,))
            if video_ids:
                self.conn.execute("INSERT INTO album_share_snapshots VALUES (?,?,?)",
                                  (browse_id, title, json.dumps(video_ids)))

    @synchronized
    def cached_album_share(self, browse_id):
        if browse_id not in self.complete_album_tracklist_ids():
            return None
        row = self.conn.execute("SELECT title,video_ids_json FROM album_share_snapshots WHERE browse_id=?",
                                (browse_id,)).fetchone()
        return {"title": row["title"], "video_ids": json.loads(row["video_ids_json"])} if row else None

    @synchronized
    def cached_album_tracklist_ids(self):
        return {r[0] for r in self.conn.execute("SELECT browse_id FROM album_tracklists")}

    @synchronized
    def complete_album_tracklist_ids(self):
        """Fetched metadata alone does not mean every track was folded into the library."""
        return {row[0] for row in self.conn.execute(
            "SELECT p.provider_id FROM album_fetch_state f JOIN album_provider_ids p "
            "ON p.album_id=f.album_id AND p.provider='youtube' WHERE f.complete=1 "
            "AND f.expected_track_count>0 AND f.expected_track_count="
            "(SELECT COUNT(*) FROM album_tracks t WHERE t.album_id=f.album_id AND t.is_full=1) "
            "AND NOT EXISTS(SELECT 1 FROM album_tracks t LEFT JOIN library_memberships m ON m.entry_id=t.entry_id "
            "WHERE t.album_id=f.album_id AND t.is_full=1 AND m.entry_id IS NULL)")}

    @synchronized
    def playback_source(self, playlist_ytm_id=None, video_id=None, album_browse_id=None) -> dict | None:
        """Resolve the actual playback context, without guessing from playlist membership."""
        if playlist_ytm_id:
            row = self.conn.execute(
                "SELECT p.id, p.title FROM playlists p JOIN identities i ON i.id=p.identity_id "
                "WHERE p.ytm_playlist_id=? ORDER BY i.is_master DESC, p.id LIMIT 1",
                (playlist_ytm_id,)).fetchone()
            if row:
                return {"kind": "playlist", "id": row["id"], "title": row["title"] or "Playlist"}
            # Album queues use OLAK ids. An unrelated playlist or radio queue must not be
            # presented as the track's album just because its songs also appear on that album.
            if not playlist_ytm_id.startswith("OLAK5uy_"):
                return None
        if not album_browse_id:
            rows = self.conn.execute(
                "SELECT DISTINCT p.provider_id FROM album_tracks a JOIN catalog_entries e ON e.id=a.entry_id "
                "JOIN album_provider_ids p ON p.album_id=a.album_id AND p.provider='youtube' WHERE e.video_id=?",
                (video_id,)).fetchall()
            album_browse_id = rows[0][0] if len(rows)==1 else None
        if album_browse_id:
            row = self.conn.execute(
                "SELECT a.title FROM albums a JOIN album_provider_ids p ON p.album_id=a.id "
                "WHERE p.provider='youtube' AND p.provider_id=?", (album_browse_id,)).fetchone()
            return {"kind": "album", "browse": album_browse_id,
                    "title": (row["title"] if row else None) or "Album"}
        return None

    # --- saved albums (mirror of the YouTube Music library, synced locally) ---
    def _start_legacy_cohort(self):
        """Freeze the required owners; added legacy evidence needs fresh observations."""
        if not self.conn.execute('SELECT 1 FROM album_library_memberships WHERE identity_id IS NULL').fetchone():
            self.conn.execute('DELETE FROM album_library_legacy_cohort')
            self.conn.execute('INSERT INTO album_library_legacy_cohort(identity_id) SELECT id FROM identities')
        else:
            self.conn.execute('UPDATE album_library_legacy_cohort SET observed_at=NULL')

    def _advance_album_revision(self, identity_id):
        # Unattributed edits affect every possible owner: a fetch admitted before
        # newly added legacy evidence cannot count as its successful observation.
        owners = [identity_id] if identity_id is not None else [r[0] for r in self.conn.execute('SELECT id FROM identities')]
        self.conn.executemany('INSERT INTO album_library_revisions VALUES (?,1) '
                              'ON CONFLICT(identity_id) DO UPDATE SET revision=revision+1', [(i,) for i in owners])

    @synchronized
    def saved_album_revision(self, identity_id):
        """Capture before remote work; snapshot commits compare under a write reservation."""
        row = self.conn.execute('SELECT revision FROM album_library_revisions WHERE identity_id=?',
                                (identity_id,)).fetchone()
        return row[0] if row else 0

    @synchronized
    def replace_saved_albums(self, albums) -> None:
        """Replace unattributed legacy data without erasing known account ownership."""
        albums = list(albums)
        with self.conn:
            self._advance_album_revision(None)
            self.conn.execute('DELETE FROM album_library_memberships WHERE identity_id IS NULL')
            self._start_legacy_cohort()
            for a in albums:
                if a.get('browse'):
                    album_catalog.save(self.conn,a)

    @synchronized
    def replace_saved_album_snapshots(self, snapshots, *, expected_revisions=None, now=None):
        """Commit complete, current snapshots; return the accepted account IDs.

        Callers fetching remotely must pass revisions captured before their fetch.
        Without revisions, this is an immediate repository membership transaction.
        """
        accepted = set()
        with self.conn:
            # Serialize the revision check and replacement across Store connections,
            # as well as threads using this repository. No network work occurs here.
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            active = {i[0] for i in self.conn.execute('SELECT id FROM identities WHERE active=1')}
            for identity_id,items in snapshots.items():
                if identity_id not in active:
                    continue
                if expected_revisions is not None and (
                        identity_id not in expected_revisions or
                        expected_revisions[identity_id]!=self.saved_album_revision(identity_id)):
                    continue
                self._advance_album_revision(identity_id)
                self.conn.execute('DELETE FROM album_library_memberships WHERE identity_id=?', (identity_id,))
                for item in items:
                    album_catalog.save(self.conn,item,identity_id=identity_id)
                observed_at = time.time() if now is None else now
                self.conn.execute('INSERT INTO album_library_snapshots VALUES (?,?) ON CONFLICT(identity_id) '
                                  'DO UPDATE SET observed_at=excluded.observed_at', (identity_id,observed_at))
                self.conn.execute('UPDATE album_library_legacy_cohort SET observed_at=? WHERE identity_id=?',
                                  (observed_at,identity_id))
                accepted.add(identity_id)
            cohort = list(self.conn.execute('SELECT observed_at FROM album_library_legacy_cohort'))
            if accepted and cohort and all(row[0] is not None for row in cohort):
                self.conn.execute('DELETE FROM album_library_memberships WHERE identity_id IS NULL')
        return accepted

    @synchronized
    def saved_album_ids(self, *, browse_ids=None) -> set:
        if browse_ids is None:
            return {r["browse_id"] for r in self.conn.execute("SELECT browse_id FROM saved_albums")}
        result = set()
        for bid in set(browse_ids):
            if self.conn.execute('SELECT 1 FROM album_provider_ids p JOIN album_library_memberships m '
                "ON m.album_id=p.album_id WHERE p.provider='youtube' AND p.provider_id=? LIMIT 1", (bid,)).fetchone():
                result.add(bid)
        return result

    @synchronized
    def album_id(self, browse_id):
        return album_catalog.lookup(self.conn,browse_id)

    @synchronized
    def add_saved_album(self, a, *, identity_id=None) -> None:
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            self._advance_album_revision(identity_id)
            if identity_id is None:
                self._start_legacy_cohort()
            album_catalog.save(self.conn,a,identity_id=identity_id)

    @synchronized
    def remove_saved_album(self, browse_id, *, identity_id=None) -> None:
        with self.conn:
            self._advance_album_revision(identity_id)
            self.conn.execute('DELETE FROM album_library_memberships WHERE album_id=? AND identity_id IS ?',
                              (album_catalog.lookup(self.conn,browse_id),identity_id))

    @synchronized
    def get_saved_albums(self, *, identity_id=None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT browse_id browse, title, artist, COALESCE(year,'') year, type, thumbnail FROM saved_albums "
            "WHERE (? IS NULL OR EXISTS (SELECT 1 FROM album_library_memberships m "
            "JOIN album_provider_ids p ON p.album_id=m.album_id AND p.provider='youtube' "
            "WHERE p.provider_id=saved_albums.browse_id AND m.identity_id=?)) "
            "ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE", (identity_id,identity_id)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def saved_album_owners(self, browse_id=None) -> dict:
        """Retain inactive and unknown owners in combined-library reads and mutations."""
        owners = {}
        for row in self.conn.execute(
                "SELECT p.provider_id browse,m.identity_id,i.label,i.active "
                "FROM album_library_memberships m JOIN album_provider_ids p "
                "ON p.album_id=m.album_id AND p.provider='youtube' "
                "LEFT JOIN identities i ON i.id=m.identity_id "
                "WHERE (? IS NULL OR p.provider_id=?) ORDER BY i.label COLLATE NOCASE,m.identity_id",
                (browse_id,browse_id)):
            owners.setdefault(row['browse'], []).append({
                'identity_id': row['identity_id'], 'label': row['label'], 'active': bool(row['active'])})
        return owners

    @synchronized
    def saved_albums_recency(self) -> dict:
        """{browse_id: newest play ts across the album's tracks | None}. Plays come from history (a
        track's identity_key in a snapshot). LEFT JOINs throughout, so an album whose tracks aren't in
        the library (or were never played) comes back None, i.e. as cold as possible."""
        rows = self.conn.execute(
            "SELECT sa.browse_id AS browse,MAX(hi.at) AS last "
            "FROM saved_albums sa "
            "LEFT JOIN album_track_links a ON a.browse_id=sa.browse_id "
            "LEFT JOIN tracks t ON t.id=a.entry_id "
            "LEFT JOIN listening_activity hi ON hi.identity_key=t.identity_key "
            "GROUP BY sa.browse_id").fetchall()
        return {r["browse"]: r["last"] for r in rows}

    # --- aggregate library views ---
    def _play_counts(self):
        return {r["identity_key"]: r["c"]
                for r in self.conn.execute("SELECT identity_key, COUNT(*) c FROM listening_days GROUP BY identity_key")}

    @synchronized
    def collection_albums(self) -> list[dict]:
        """Every album across your playlists: artist, song count, #playlists, total plays."""
        plays = self._play_counts()
        rows = self.conn.execute(
            "SELECT a.id album_id,a.title album,t.identity_key key,a.artist artist,p.provider_id browse, "
            "       MIN(t.thumbnail) thumb, GROUP_CONCAT(DISTINCT pt.playlist_id) pls "
            "FROM albums a JOIN album_tracks at ON at.album_id=a.id JOIN tracks t ON t.id=at.entry_id "
            "JOIN playlist_tracks pt ON pt.track_id=t.id JOIN album_provider_ids p "
            "ON p.album_id=a.id AND p.provider='youtube' WHERE NULLIF(a.title,'') IS NOT NULL "
            "GROUP BY a.id,t.identity_key").fetchall()
        albums = {}
        for r in rows:
            a = albums.setdefault(r['album_id'], {"album": r["album"], "artist": r["artist"],
                                               "browse": r["browse"], "thumb": None,
                                               "songs": 0, "plays": 0, "_pls": set()})
            a["songs"] += 1
            a["plays"] += plays.get(r["key"], 0)
            a["thumb"] = a["thumb"] or r["thumb"]
            a["_pls"].update((r["pls"] or "").split(","))
        out = []
        for a in albums.values():
            a["n_pls"] = len([x for x in a.pop("_pls") if x])
            out.append(a)
        out.sort(key=lambda a: (-a["plays"], a["album"].lower()))
        return out

    @synchronized
    def collection_artists(self) -> list[dict]:
        """Artists across playlists and saved albums, including albums not yet materialized."""
        plays = self._play_counts()
        rows = self.conn.execute(
            "SELECT CASE WHEN (SELECT COUNT(*) FROM track_artists x WHERE x.entry_id=t.id)=1 "
            "THEN t.artist ELSE ta.credit END artist,ta.artist_id,t.identity_key key,a.id album_id,ap.provider_id browse, "
            "       p.provider_id album_browse,MIN(NULLIF(t.thumbnail,'')) thumb, "
            "       GROUP_CONCAT(DISTINCT pt.playlist_id) pls "
            "FROM tracks t JOIN track_artists ta ON ta.entry_id=t.id LEFT JOIN artist_provider_ids ap "
            "ON ap.artist_id=ta.artist_id AND ap.provider='youtube' "
            "LEFT JOIN album_tracks at ON at.entry_id=t.id LEFT JOIN albums a ON a.id=at.album_id "
            "LEFT JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
            "LEFT JOIN playlist_tracks pt ON pt.track_id=t.id "
            "WHERE t.artist<>'' AND (pt.track_id IS NOT NULL OR "
            "EXISTS(SELECT 1 FROM album_library_memberships m WHERE m.album_id=a.id)) "
            "GROUP BY ta.artist_id,t.identity_key,a.id").fetchall()
        artists = {}

        def artist_row(artist, browse=None, artist_id=None):
            if browse and artist_id is None:
                ref = self.conn.execute("SELECT artist_id FROM artist_provider_ids WHERE provider='youtube' AND provider_id=?", (browse,)).fetchone()
                artist_id = ref[0] if ref else None
            artist_id = artist_id or artist_catalog.lookup(self.conn,artist)
            return artists.setdefault(artist_id or ('unidentified',artist), {
                "artist": artist, "artist_id":artist_id,"browse": browse, "plays": 0, "thumbnail": None,
                "_songs": set(), "_albums": set(), "_pls": set()})

        for r in rows:
            a = artist_row(r["artist"], r["browse"],r['artist_id'])
            if r["key"] not in a["_songs"]:
                a["plays"] += plays.get(r["key"], 0)
            a["_songs"].add(r["key"])
            if r['album_browse']:
                a['_albums'].add(r['album_id'])
            a["thumbnail"] = a["thumbnail"] or r["thumb"]
            a["_pls"].update((r["pls"] or "").split(","))

        for album in self.conn.execute(
                'SELECT DISTINCT a.id album_id,a.thumbnail,aa.artist_id,aa.credit artist,ap.provider_id browse '
                'FROM albums a JOIN album_library_memberships m ON m.album_id=a.id '
                'JOIN album_artists aa ON aa.album_id=a.id LEFT JOIN artist_provider_ids ap '
                "ON ap.artist_id=aa.artist_id AND ap.provider='youtube'"):
            a = artist_row(album['artist'],album['browse'],album['artist_id'])
            a['_albums'].add(album['album_id'])
            a['thumbnail'] = a['thumbnail'] or album['thumbnail']

        out = []
        for a in artists.values():
            a["songs"] = len(a.pop("_songs"))
            a['n_albums'] = len(a.pop('_albums'))
            a["n_pls"] = len([x for x in a.pop("_pls") if x])
            out.append(a)
        out.sort(key=lambda a: (-a["plays"], a["artist"].lower()))
        return out

    @synchronized
    def artist_albums(self, artist, browse_id=None):
        if browse_id:
            row = self.conn.execute("SELECT artist_id FROM artist_provider_ids WHERE provider='youtube' AND provider_id=?", (browse_id,)).fetchone()
            aid = row[0] if row else None
        else:
            aid = artist_catalog.lookup(self.conn,artist)
        if aid is None:
            return []
        plays, collection = self._play_counts(), {}
        for row in self.conn.execute(
                "SELECT a.id,a.title album,a.year,a.thumbnail thumb,p.provider_id browse, "
                "EXISTS(SELECT 1 FROM album_library_memberships m WHERE m.album_id=a.id) saved "
                "FROM albums a JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
                "WHERE EXISTS(SELECT 1 FROM album_tracks at JOIN tracks t ON t.id=at.entry_id "
                "JOIN track_artists ta ON ta.entry_id=t.id WHERE at.album_id=a.id AND ta.artist_id=?) "
                "OR (EXISTS(SELECT 1 FROM album_library_memberships m WHERE m.album_id=a.id) "
                "AND EXISTS(SELECT 1 FROM album_artists aa WHERE aa.album_id=a.id AND aa.artist_id=?))", (aid,aid)):
            collection[row['id']] = {**dict(row),'songs':0,'plays':0,'_pls':set()}
        if self.conn.execute(
                "SELECT 1 FROM tracks t JOIN track_artists ta ON ta.entry_id=t.id WHERE ta.artist_id=? AND NULLIF(TRIM(t.album),'') IS NULL "
                'AND NOT EXISTS(SELECT 1 FROM album_tracks at WHERE at.entry_id=t.id) LIMIT 1', (aid,)).fetchone():
            collection[None] = dict(id=None,album='Singles / no album',year=None,thumb=None,
                                    browse=None,saved=False,songs=0,plays=0,_pls=set())
        for row in self.conn.execute(
                "SELECT at.album_id,t.identity_key,MIN(NULLIF(t.thumbnail,'')) thumb, "
                "GROUP_CONCAT(DISTINCT pt.playlist_id) pls FROM tracks t LEFT JOIN album_tracks at ON at.entry_id=t.id "
                "JOIN track_artists ta ON ta.entry_id=t.id LEFT JOIN playlist_tracks pt ON pt.track_id=t.id "
                "WHERE ta.artist_id=? AND (at.album_id IS NOT NULL OR NULLIF(TRIM(t.album),'') IS NULL) "
                "GROUP BY at.album_id,t.identity_key", (aid,)):
            album = collection.get(row['album_id'])
            if album is None:
                continue  # Historical title-only labels are metadata, not identified releases.
            album['songs'] += 1
            album['plays'] += plays.get(row['identity_key'],0)
            album['thumb'] = album['thumb'] or row['thumb']
            album['_pls'].update(filter(None,(row['pls'] or '').split(',')))
        for album in collection.values():
            album['n_pls'] = len(album.pop('_pls'))
            album['saved'] = bool(album['saved'])
        return sorted(collection.values(),key=lambda a:(-a['plays'],(a['album'] or '').casefold(),a['id'] or 0))

    @synchronized
    def artist_browse_id(self, artist):
        """The unambiguous artist identity's YouTube channel/browse ID, or None."""
        aid = artist_catalog.lookup(self.conn,artist)
        r = self.conn.execute("SELECT provider_id b FROM artist_provider_ids WHERE artist_id=? AND provider='youtube'", (aid,)).fetchone()
        return r["b"] if r else None
