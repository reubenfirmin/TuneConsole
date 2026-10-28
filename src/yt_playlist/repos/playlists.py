"""PlaylistRepo: playlists, their track membership/ordering, groups, and hidden flags."""
from yt_playlist.core.playlist_scopes import ensure,resolve

from yt_playlist.repos.base import Repo, synchronized, snapshot_read, GENERATED_GROUP
from yt_playlist.repos.models import Playlist, PlaylistMemberships


_FIRST_TRACK_THUMBNAIL = (
    "(SELECT t.thumbnail FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
    "WHERE pt.playlist_id=p.id AND NULLIF(t.thumbnail, '') IS NOT NULL "
    "ORDER BY pt.position, pt.track_id LIMIT 1)"
)


class PlaylistRepo(Repo):
    @synchronized
    def playlist_reference(self, ytm, identity_id=None):
        return resolve(self.conn,ytm,identity_id)

    @synchronized
    def playlist_provenance(self, ytm, *, identity_id=None) -> dict:
        ytm = resolve(self.conn,ytm,identity_id)
        row = self.conn.execute('SELECT origin,adopted,description FROM playlist_provenance WHERE ytm=?',
                                (ytm,)).fetchone()
        return dict(row) if row else {'origin': 'ordinary', 'adopted': 1, 'description': None}

    @synchronized
    def set_playlist_provenance(self, ytm, *, origin, adopted, description=None, identity_id=None) -> None:
        with self.conn:
            ytm = ensure(self.conn,ytm,identity_id)
            self.conn.execute('INSERT INTO playlist_provenance VALUES (?,?,?,?) '
                              'ON CONFLICT(ytm) DO UPDATE SET origin=excluded.origin,'
                              'adopted=excluded.adopted,description=excluded.description',
                              (ytm, origin, int(adopted), description))

    @synchronized
    def mark_generated_playlist(self, ytm, description=None) -> None:
        from yt_playlist.library.playlist_metadata import GENERATED_DESCRIPTION
        self.set_playlist_provenance(ytm, origin='tuneconsole', adopted=False,
                                     description=description or GENERATED_DESCRIPTION)

    @synchronized
    def adopt_generated_playlist(self, ytm) -> None:
        from yt_playlist.library.playlist_metadata import PROMOTED_DESCRIPTION
        with self.conn:
            ytm = ensure(self.conn,ytm)
            self.conn.execute("UPDATE playlist_provenance SET adopted=1,description=? WHERE ytm=? "
                              "AND origin='tuneconsole'", (PROMOTED_DESCRIPTION, ytm))

    @synchronized
    def is_quarantined_playlist(self, ytm, *, identity_id=None) -> bool:
        ytm = resolve(self.conn,ytm,identity_id)
        return self.conn.execute('SELECT 1 FROM quarantined_playlists WHERE ytm=?', (ytm,)).fetchone() is not None

    @synchronized
    def reconcile_playlist_provenance(self, ytm, description, *, identity_id=None) -> None:
        from yt_playlist.library.playlist_metadata import is_generated_description, PROMOTED_DESCRIPTION
        ytm = resolve(self.conn,ytm,identity_id)
        current = self.playlist_provenance(ytm)
        promoted = PROMOTED_DESCRIPTION.casefold() in (description or '').casefold()
        generated = is_generated_description(description)
        if generated or promoted or current['origin'] == 'tuneconsole':
            # A stale marker cannot undo explicit local adoption. An absent description is
            # not an adoption decision, including incomplete sync responses.
            adopted = promoted or (current['origin'] == 'tuneconsole' and current['adopted'])
            self.set_playlist_provenance(ytm, origin='tuneconsole', adopted=adopted,
                                         description=description if description is not None else current['description'])

    @synchronized
    def playlist_kind(self, playlist_id) -> str:
        """Classify a playlist by its tracks' YouTube videoType.

        Returns 'audio' (all ATV), 'video' (all OMV/UGC/…), 'mixed' (both), 'mix' (has tracks but
        YouTube tagged none, i.e. an auto-generated radio/mix playlist), or '' (no tracks).
        """
        rows = self.conn.execute(
            "SELECT t.video_type FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=?", (playlist_id,)).fetchall()
        if not rows:
            return ""
        kinds = {"audio" if r["video_type"] == "MUSIC_VIDEO_TYPE_ATV" else "video"
                 for r in rows if r["video_type"] is not None}
        if not kinds:
            return "mix"        # non-empty but entirely untyped -> auto-mix / radio
        if kinds == {"audio"}:
            return "audio"
        if kinds == {"video"}:
            return "video"
        return "mixed"

    @synchronized
    def upsert_playlist(self, identity_id, ytm_playlist_id, title, track_count, content_hash, now,
                        thumbnail=None, *, created_at=None) -> int:
        """Sync metadata, preserving the actual creation date when one is known."""
        with self.conn:
            return self._upsert_playlist(identity_id, ytm_playlist_id, title, track_count,
                                         content_hash, now, thumbnail, created_at=created_at)

    def _upsert_playlist(self, identity_id, ytm_playlist_id, title, track_count, content_hash, now,
                         thumbnail=None, *, created_at=None):
        """Caller owns the transaction; never commit a partial snapshot."""
        ref = ensure(self.conn,ytm_playlist_id,identity_id)
        row = self.conn.execute(
            "SELECT id, first_seen, content_hash, last_changed FROM playlists "
            "WHERE identity_id=? AND ytm_playlist_id=?", (identity_id, ytm_playlist_id)).fetchone()
        if row is None:
            membership_id = self.conn.execute(
                'INSERT INTO playlist_membership_refs(scope_ref,identity_id) VALUES (?,?)',
                (ref,identity_id)).lastrowid
            self.conn.execute(
                "INSERT INTO playlists(id,identity_id,ytm_playlist_id,title,track_count,"
                "content_hash,first_seen,last_seen,last_changed,thumbnail,created_at,created_at_source,scope_ref) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (membership_id,identity_id, ytm_playlist_id, title, track_count, content_hash, now, now, now, thumbnail,
                 created_at, "local" if created_at is not None else None,ref))
            return membership_id
        last_changed = now if row["content_hash"] != content_hash else row["last_changed"]
        self.conn.execute(
            "UPDATE playlists SET title=?, track_count=?, content_hash=?, last_seen=?, last_changed=?, "
            "thumbnail=COALESCE(NULLIF(?, ''), thumbnail), created_at=COALESCE(created_at, ?), "
            "created_at_source=COALESCE(created_at_source, ?) WHERE id=?",
            (title, track_count, content_hash, now, last_changed, thumbnail, created_at,
             "local" if created_at is not None else None, row["id"]))
        return row["id"]

    @synchronized
    def set_playlist_tracks(self, playlist_id, track_ids) -> None:
        with self.conn:
            self._set_playlist_tracks(playlist_id, track_ids)

    def _set_playlist_tracks(self, playlist_id, track_ids):
        from yt_playlist.core.catalog import resolve_entry
        # de-dupe by track id, keeping first position: YouTube's get_playlist can return the same
        # video many times (pagination duplication), which would otherwise inflate the playlist.
        seen = set()
        resolved = [resolve_entry(self.conn,t) for t in track_ids]
        unique = [t for t in resolved if not (t in seen or seen.add(t))]
        self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
        self.conn.executemany(
            "INSERT INTO playlist_tracks(playlist_id,track_id,position) VALUES (?,?,?)",
            [(playlist_id, tid, pos) for pos, tid in enumerate(unique)])

    @synchronized
    def playlist_revision(self, identity_id, ytm_playlist_id):
        """Capture before remote work; retained tokens also detect delete/recreate."""
        ref = resolve(self.conn, ytm_playlist_id, identity_id)
        row = self.conn.execute('SELECT revision FROM playlist_library_revisions '
                                'WHERE scope_ref=? AND identity_id=?', (ref, identity_id)).fetchone()
        return row[0] if row else 0

    @synchronized
    def playlist_revisions(self, identity_id):
        """Include tombstones before a library listing supplies remote titles/absence."""
        return {r['scope_ref']: r['revision'] for r in self.conn.execute(
            'SELECT scope_ref,revision FROM playlist_library_revisions WHERE identity_id=?', (identity_id,))}

    @synchronized
    def publish_playlist_snapshot(self, identity_id, ytm_playlist_id, title, track_ids, now, *,
                                  expected_revision, thumbnail=None, created_at=None):
        """Publish complete remote metadata/membership iff its pre-fetch owner survives.

        Catalog observations may be retained before this call, but acquire library
        membership only on acceptance. Network I/O must remain outside this transaction.
        A rejected read returns None and cannot advance freshness or placement work.
        """
        from yt_playlist.core.catalog import resolve_entry
        from yt_playlist.library.playlist_metadata import content_hash

        if self.conn.in_transaction:
            raise RuntimeError('Cannot publish a playlist inside another transaction')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            if self.playlist_revision(identity_id, ytm_playlist_id) != expected_revision:
                return None
            ids = list(dict.fromkeys(resolve_entry(self.conn, t) for t in track_ids))
            keys = [self.conn.execute('SELECT identity_key FROM catalog_tracks WHERE id=?',
                                     (tid,)).fetchone()[0] for tid in ids]
            pid = self._upsert_playlist(identity_id, ytm_playlist_id, title, len(ids), content_hash(keys),
                                        now, thumbnail, created_at=created_at)
            self._set_playlist_tracks(pid, ids)
            self.conn.executemany("INSERT OR IGNORE INTO library_memberships VALUES (?,'observed',?)",
                                  [(tid, now) for tid in ids])
            return pid

    @synchronized
    def publish_confirmed_tracks(self, playlist_id, title, tracks, now, *, expected_prefix=None, destination=None):
        """Atomically mirror a confirmed remote sequence and its catalog metadata.

        A caller may supply an unchanged confirmed prefix. Use incremental writes
        only when the saved sequence still matches it; otherwise reconcile fully.
        No nested repository calls may commit part of this publication.
        """
        from yt_playlist.core import catalog
        from yt_playlist.library.playlist_metadata import content_hash

        tracks = list(tracks)
        videos = [t["video_id"] for t in tracks]
        prefix = list(expected_prefix) if expected_prefix is not None else None
        if any(not video for video in videos) or len(set(videos)) != len(videos):
            raise ValueError("Confirmed playlist must contain distinct playable uploads")
        if self.conn.in_transaction:
            raise RuntimeError("Cannot publish a playlist inside another transaction")
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if playlist_id is None:
                if destination is None:
                    raise ValueError("A destination account and playlist are required")
                identity_id, remote_id = destination
                row = self.conn.execute(
                    'SELECT id FROM playlists WHERE identity_id=? AND ytm_playlist_id=?',
                    (identity_id, remote_id)).fetchone()
                playlist_id = row[0] if row else self._upsert_playlist(
                    identity_id, remote_id, title, 0, content_hash([]), now)
            if not self.conn.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
                raise ValueError("Unknown playlist")
            saved = self.conn.execute(
                "SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position",
                (playlist_id,)).fetchall()
            incremental = (prefix is not None
                           and [r["video_id"] for r in saved] == prefix
                           and videos[:len(saved)] == prefix
                           and not self.conn.execute(
                               "SELECT 1 FROM unresolved_playlist_items WHERE playlist_id=? LIMIT 1",
                               (playlist_id,)).fetchone())
            start = len(saved) if incremental else 0
            if not incremental:
                self.conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
                self.conn.execute("DELETE FROM unresolved_playlist_items WHERE playlist_id=?", (playlist_id,))
            for position, track in enumerate(tracks[start:], start):
                catalog.upsert(
                    self.conn, track["video_id"], track.get("title") or "", track.get("artist") or "",
                    track.get("album") or "", track.get("duration"), thumbnail=track.get("thumbnail"),
                    library=True)
                self.conn.execute("INSERT INTO playlist_items VALUES (?,?,?)",
                                  (playlist_id, track["video_id"], position))
            # A new upload can disambiguate an older recording's text key.
            # Hash the final catalog identities after every upsert has settled.
            keys = self.conn.execute(
                "SELECT r.identity_key FROM playlist_items p "
                "JOIN youtube_items y ON y.video_id=p.video_id JOIN recordings r ON r.id=y.recording_id "
                "WHERE p.playlist_id=?", (playlist_id,)).fetchall()
            digest = content_hash({row[0] for row in keys})
            self.conn.execute(
                "UPDATE playlists SET title=?,track_count=?,content_hash=?,last_seen=?,"
                "last_changed=CASE WHEN content_hash<>? THEN ? ELSE last_changed END WHERE id=?",
                (title, len(tracks), digest, now, digest, now, playlist_id))
            self.conn.execute(
                f"UPDATE playlists AS p SET thumbnail={_FIRST_TRACK_THUMBNAIL} "
                "WHERE p.id=? AND NULLIF(p.thumbnail, '') IS NULL", (playlist_id,))
        return playlist_id

    @synchronized
    def set_song_liked(self, identity_id, video_id, on) -> None:
        """Reflect a like/unlike locally by toggling the song's membership in this identity's
        Liked Music (LM) playlist, so the derived `liked` flag flips immediately (a later sync
        reconciles with YouTube). No-op if the identity has no synced LM playlist yet."""
        lm = self.conn.execute(
            "SELECT id FROM playlists WHERE identity_id=? AND ytm_playlist_id='LM'", (identity_id,)).fetchone()
        row = self.conn.execute(
            "SELECT id, identity_key FROM tracks WHERE video_id=? ORDER BY id DESC LIMIT 1", (video_id,)).fetchone()
        if lm is None or row is None:
            return
        lm_id, tid, key = lm["id"], row["id"], row["identity_key"]
        with self.conn:
            if on:
                self.conn.execute(
                    "INSERT INTO playlist_tracks(playlist_id, track_id, position) "
                    "SELECT ?,?,(SELECT IFNULL(MAX(position),-1)+1 FROM playlist_tracks WHERE playlist_id=?) "
                    "WHERE NOT EXISTS(SELECT 1 FROM playlist_tracks WHERE playlist_id=? AND track_id=?)",
                    (lm_id, tid, lm_id, lm_id, tid))
            else:
                self.conn.execute(
                    "DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id IN "
                    "(SELECT id FROM tracks WHERE video_id=?)", (lm_id, video_id))

    @synchronized
    def get_playlist_track_ids(self, playlist_id) -> list:
        rows = self.conn.execute(
            "SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
            (playlist_id,)).fetchall()
        return [r["track_id"] for r in rows]

    @synchronized
    def playlist_ids_for_video(self, video_id) -> set[int]:
        """Exact-video membership for the song destination picker, without loading every track."""
        rows = self.conn.execute(
            "SELECT DISTINCT pt.playlist_id FROM playlist_tracks pt "
            "JOIN tracks t ON t.id=pt.track_id WHERE t.video_id=?", (video_id,)).fetchall()
        return {r["playlist_id"] for r in rows}

    @synchronized
    def set_playlist_title(self, playlist_id, title, now) -> None:
        self.conn.execute("UPDATE playlists SET title=?, last_changed=? WHERE id=?",
                          (title, now, playlist_id))
        self.conn.commit()

    @synchronized
    def set_playlist_track_count(self, playlist_id, count, now) -> None:
        with self.conn:
            self.conn.execute("UPDATE playlists SET track_count=?, last_changed=?, last_seen=? WHERE id=?",
                              (count, now, now, playlist_id))

    @synchronized
    def remove_playlist(self, playlist_id, *, require_empty=False, expected_revision=None):
        """Remove this account membership. Durable scope and curation survive reappearance."""
        with self.conn:
            if expected_revision is not None:
                self.conn.execute('BEGIN IMMEDIATE')
                owner = self.conn.execute('SELECT identity_id,ytm_playlist_id FROM playlists WHERE id=?',
                                          (playlist_id,)).fetchone()
                if owner is None or self.playlist_revision(*owner) != expected_revision:
                    return False
            if require_empty:
                playlist = self.conn.execute(
                    "SELECT track_count FROM playlists WHERE id=?", (playlist_id,)).fetchone()
                if playlist is None:
                    raise ValueError("playlist no longer exists")
                if playlist[0] != 0 or self.conn.execute(
                        "SELECT 1 FROM playlist_tracks WHERE playlist_id=? LIMIT 1",
                        (playlist_id,)).fetchone():
                    raise ValueError("This playlist has cached tracks. Re-sync before removing it.")
            self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
            self.conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
            return True

    @synchronized
    def get_playlists(self) -> list[Playlist]:
        rows = self.conn.execute("SELECT * FROM playlists").fetchall()
        return [Playlist(r["id"], r["identity_id"], r["ytm_playlist_id"], r["title"],
                         r["track_count"], r["content_hash"], r["first_seen"],
                         r["last_seen"], r["last_changed"], r["thumbnail"], r["created_at"],
                         r["created_at_source"]) for r in rows]

    @synchronized
    def get_playlist_thumbnails(self) -> dict[int, str | None]:
        """Covers for the playlist list, falling back to the first song with artwork."""
        rows = self.conn.execute(
            f"SELECT p.id, COALESCE(NULLIF(p.thumbnail, ''), {_FIRST_TRACK_THUMBNAIL}) "
            "AS thumbnail FROM playlists p"
        ).fetchall()
        return {r["id"]: r["thumbnail"] for r in rows}

    @synchronized
    def ensure_playlist_thumbnail(self, playlist_id) -> None:
        """Save initial cover art after songs are added, without replacing an existing cover.

        Use persisted membership so rejected songs cannot supply the cover. A later
        YouTube sync can replace this initial artwork with its canonical playlist cover.
        """
        with self.conn:
            self.conn.execute(
                f"UPDATE playlists AS p SET thumbnail={_FIRST_TRACK_THUMBNAIL} "
                "WHERE p.id=? AND NULLIF(p.thumbnail, '') IS NULL", (playlist_id,))

    @synchronized
    def home_preview(self, playlist_id):
        """Current metadata and all current members in playlist order for Home."""
        row = self.conn.execute('SELECT * FROM playlists WHERE id=?', (playlist_id,)).fetchone()
        if (not row or not row['track_count'] or self.is_quarantined_playlist(row['scope_ref'])):
            return None
        tracks = [dict(r) for r in self.conn.execute(
            'SELECT t.identity_key key,t.title,t.artist,t.video_id,t.thumbnail FROM playlist_tracks p '
            'JOIN catalog_tracks t ON t.id=p.track_id WHERE p.playlist_id=? '
            'ORDER BY p.position', (playlist_id,))]
        if not tracks:
            return None
        return {'id': row['id'], 'ytm': row['ytm_playlist_id'], 'title': row['title'],
                'track_count': row['track_count'], 'thumbnail': row['thumbnail'], 'tracks': tracks}

    @synchronized
    def get_playlist(self, playlist_id) -> Playlist | None:
        row = self.conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        return None if row is None else Playlist(
            row["id"], row["identity_id"], row["ytm_playlist_id"], row["title"],
            row["track_count"], row["content_hash"], row["first_seen"],
            row["last_seen"], row["last_changed"], row["thumbnail"], row["created_at"], row["created_at_source"])

    @synchronized
    def get_playlist_for_account(self, identity_id, ytm_playlist_id) -> Playlist | None:
        """Resolve a remote target afresh after sync may have replaced its local row."""
        row = self.conn.execute(
            "SELECT id FROM playlists WHERE identity_id=? AND ytm_playlist_id=?",
            (identity_id, ytm_playlist_id)).fetchone()
        return self.get_playlist(row[0]) if row else None

    @synchronized
    def get_playlist_track_keys(self, playlist_id) -> set[str]:
        rows = self.conn.execute(
            "SELECT t.identity_key FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=?", (playlist_id,)).fetchall()
        return {r["identity_key"] for r in rows}

    @snapshot_read
    def cleanup_memberships(self) -> PlaylistMemberships:
        """Bulk membership read for Cleanup, independent of pair-analysis policy.

        Count unique recordings as get_playlist_track_keys does. Kinds still use
        every upload, including multiple uploads of the same recording.
        """
        playlists = self.get_playlists()
        keys = {p.id: set() for p in playlists}
        types = {p.id: set() for p in playlists}
        for row in self.conn.execute(
                'SELECT pt.playlist_id,t.identity_key,t.video_type FROM playlist_tracks pt '
                'JOIN tracks t ON t.id=pt.track_id').fetchall():
            keys[row['playlist_id']].add(row['identity_key'])
            if row['video_type'] is not None:
                types[row['playlist_id']].add(
                    'audio' if row['video_type'] == 'MUSIC_VIDEO_TYPE_ATV' else 'video')
        kinds = {p.scope_ref: ('mixed' if len(types[p.id]) > 1 else
                              next(iter(types[p.id])) if types[p.id] else
                              'mix' if keys[p.id] else '') for p in playlists}
        excluded = {r[0] for r in self.conn.execute(
            'SELECT p.id FROM playlists p JOIN quarantined_playlists q ON q.ytm=p.scope_ref '
            'WHERE q.name=?', (GENERATED_GROUP,))}
        return PlaylistMemberships(playlists, {pid: frozenset(k) for pid, k in keys.items()},
                                   kinds, excluded)

    @synchronized
    def get_playlist_tracks_with_meta(self, playlist_id) -> list[tuple]:
        rows = self.conn.execute(
            "SELECT t.identity_key, t.video_id, t.title, t.artist, t.duration_s, t.available "
            "FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? ORDER BY pt.position", (playlist_id,)).fetchall()
        return [(r["identity_key"], r["video_id"], r["title"], r["artist"], r["duration_s"], r["available"])
                for r in rows]

    @synchronized
    def set_playlist_group(self, ytm, name, *, identity_id=None) -> None:
        ytm = ensure(self.conn,ytm,identity_id)
        name = (name or "").strip()
        if name:
            self.conn.execute("INSERT OR REPLACE INTO playlist_group(ytm,name) VALUES (?,?)", (ytm, name))
        else:   # empty name clears the assignment
            self.conn.execute("DELETE FROM playlist_group WHERE ytm=?", (ytm,))
        self.conn.commit()

    @synchronized
    def hide_playlist(self, ytm, *, identity_id=None) -> None:
        ytm = ensure(self.conn,ytm,identity_id)
        self.conn.execute("INSERT OR IGNORE INTO hidden_playlists(ytm) VALUES (?)", (ytm,))
        self.conn.commit()

    @synchronized
    def unhide_playlist(self, ytm, *, identity_id=None) -> None:
        ytm = ensure(self.conn,ytm,identity_id)
        self.conn.execute("DELETE FROM hidden_playlists WHERE ytm=?", (ytm,))
        self.conn.commit()

    @synchronized
    def get_hidden_playlists(self) -> set:
        return {r["ytm"] for r in self.conn.execute("SELECT ytm FROM hidden_playlists")}

    @synchronized
    def get_playlist_groups(self) -> dict:
        return {r["ytm"]: r["name"] for r in self.conn.execute("SELECT ytm,name FROM playlist_group")}
