"""PlaylistRepo: playlists, their track membership/ordering, groups, and hidden flags."""
from yt_playlist.core.playlist_scopes import ensure,resolve

from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.models import Playlist


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
        ref = ensure(self.conn,ytm_playlist_id,identity_id)
        row = self.conn.execute(
            "SELECT id, first_seen, content_hash, last_changed FROM playlists "
            "WHERE identity_id=? AND ytm_playlist_id=?", (identity_id, ytm_playlist_id)).fetchone()
        if row is None:
            with self.conn:
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
        self.conn.commit()
        return row["id"]

    @synchronized
    def set_playlist_tracks(self, playlist_id, track_ids) -> None:
        from yt_playlist.core.catalog import resolve_entry
        # de-dupe by track id, keeping first position: YouTube's get_playlist can return the same
        # video many times (pagination duplication), which would otherwise inflate the playlist.
        seen = set()
        resolved = [resolve_entry(self.conn,t) for t in track_ids]
        unique = [t for t in resolved if not (t in seen or seen.add(t))]
        with self.conn:
            self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
            self.conn.executemany(
                "INSERT INTO playlist_tracks(playlist_id,track_id,position) VALUES (?,?,?)",
                [(playlist_id, tid, pos) for pos, tid in enumerate(unique)])

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
    def remove_playlist(self, playlist_id) -> None:
        """Remove this account membership. Durable scope and curation survive reappearance."""
        with self.conn:
            self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
            self.conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))

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
    def get_playlist(self, playlist_id) -> Playlist | None:
        row = self.conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        return None if row is None else Playlist(
            row["id"], row["identity_id"], row["ytm_playlist_id"], row["title"],
            row["track_count"], row["content_hash"], row["first_seen"],
            row["last_seen"], row["last_changed"], row["thumbnail"], row["created_at"], row["created_at_source"])

    @synchronized
    def get_playlist_track_keys(self, playlist_id) -> set[str]:
        rows = self.conn.execute(
            "SELECT t.identity_key FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=?", (playlist_id,)).fetchall()
        return {r["identity_key"] for r in rows}

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
