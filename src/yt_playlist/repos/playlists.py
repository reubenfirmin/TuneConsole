"""PlaylistRepo: playlists, their track membership/ordering, groups, and hidden flags."""
import json

from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.models import Playlist


class PlaylistRepo(Repo):
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
                        thumbnail=None) -> int:
        row = self.conn.execute(
            "SELECT id, first_seen, content_hash, last_changed FROM playlists "
            "WHERE identity_id=? AND ytm_playlist_id=?", (identity_id, ytm_playlist_id)).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO playlists(identity_id,ytm_playlist_id,title,track_count,"
                "content_hash,first_seen,last_seen,last_changed,thumbnail) VALUES (?,?,?,?,?,?,?,?,?)",
                (identity_id, ytm_playlist_id, title, track_count, content_hash, now, now, now, thumbnail))
            self.conn.commit()
            return cur.lastrowid
        last_changed = now if row["content_hash"] != content_hash else row["last_changed"]
        self.conn.execute(
            "UPDATE playlists SET title=?, track_count=?, content_hash=?, last_seen=?, last_changed=?, "
            "thumbnail=COALESCE(?, thumbnail) WHERE id=?",
            (title, track_count, content_hash, now, last_changed, thumbnail, row["id"]))
        self.conn.commit()
        return row["id"]

    @synchronized
    def set_playlist_tracks(self, playlist_id, track_ids) -> None:
        # de-dupe by track id, keeping first position: YouTube's get_playlist can return the same
        # video many times (pagination duplication), which would otherwise inflate the playlist.
        seen = set()
        unique = [t for t in track_ids if not (t in seen or seen.add(t))]
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
            else:   # drop every copy of this song (by identity_key) from this identity's LM
                self.conn.execute(
                    "DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id IN "
                    "(SELECT id FROM tracks WHERE identity_key=?)", (lm_id, key))

    @synchronized
    def get_playlist_track_ids(self, playlist_id) -> list:
        rows = self.conn.execute(
            "SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
            (playlist_id,)).fetchall()
        return [r["track_id"] for r in rows]

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
        """Drop a playlist, its track links, and any cleanup/overlap prefs that referenced it.

        Pruning the suppress/ignore/keep rows keeps stale pairs (one side deleted) from
        lingering in the Hidden/Ignored sections. The same goes for the cleanup dismissals:
        a per-playlist empty/tiny ignore is meaningless once the playlist is gone, and an
        ignored merge that lost a member can never match its signature again (the cleanup
        page would hide it forever with no way to restore it).

        We deliberately KEEP the playlist's group assignment (playlist_group, keyed by the YouTube
        id): groups are user curation that can't be reconstructed from YouTube, and a playlist that
        disappears (a transient sync, or one re-added later) should get its group back automatically.
        An orphaned group row is harmless. It just isn't shown until a matching playlist exists.
        """
        with self.conn:
            row = self.conn.execute("SELECT ytm_playlist_id FROM playlists WHERE id=?",
                                    (playlist_id,)).fetchone()
            ytm = row["ytm_playlist_id"] if row else None
            self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
            self.conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
            if ytm is not None:
                self.conn.execute("DELETE FROM suppressed_overlaps WHERE a=? OR b=?", (ytm, ytm))
                self.conn.execute("DELETE FROM overlap_ignored WHERE ytm=?", (ytm,))
                self.conn.execute("DELETE FROM overlap_kept WHERE a=? OR b=?", (ytm, ytm))
                self.conn.execute("DELETE FROM cleanup_ignored WHERE ytm=?", (ytm,))
                # members is a JSON list of ytm ids, so match in Python rather than by signature
                stale = [r["signature"]
                         for r in self.conn.execute("SELECT signature, members FROM ignored_merges")
                         if ytm in json.loads(r["members"])]
                for sig in stale:
                    self.conn.execute("DELETE FROM ignored_merges WHERE signature=?", (sig,))

    @synchronized
    def get_playlists(self) -> list[Playlist]:
        rows = self.conn.execute("SELECT * FROM playlists").fetchall()
        return [Playlist(r["id"], r["identity_id"], r["ytm_playlist_id"], r["title"],
                         r["track_count"], r["content_hash"], r["first_seen"],
                         r["last_seen"], r["last_changed"], r["thumbnail"]) for r in rows]

    @synchronized
    def get_playlist(self, playlist_id) -> Playlist | None:
        row = self.conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        return None if row is None else Playlist(
            row["id"], row["identity_id"], row["ytm_playlist_id"], row["title"],
            row["track_count"], row["content_hash"], row["first_seen"],
            row["last_seen"], row["last_changed"], row["thumbnail"])

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
    def set_playlist_group(self, ytm, name) -> None:
        name = (name or "").strip()
        if name:
            self.conn.execute("INSERT OR REPLACE INTO playlist_group(ytm,name) VALUES (?,?)", (ytm, name))
        else:   # empty name clears the assignment
            self.conn.execute("DELETE FROM playlist_group WHERE ytm=?", (ytm,))
        self.conn.commit()

    @synchronized
    def hide_playlist(self, ytm) -> None:
        self.conn.execute("INSERT OR IGNORE INTO hidden_playlists(ytm) VALUES (?)", (ytm,))
        self.conn.commit()

    @synchronized
    def unhide_playlist(self, ytm) -> None:
        self.conn.execute("DELETE FROM hidden_playlists WHERE ytm=?", (ytm,))
        self.conn.commit()

    @synchronized
    def get_hidden_playlists(self) -> set:
        return {r["ytm"] for r in self.conn.execute("SELECT ytm FROM hidden_playlists")}

    @synchronized
    def get_playlist_groups(self) -> dict:
        return {r["ytm"]: r["name"] for r in self.conn.execute("SELECT ytm,name FROM playlist_group")}
