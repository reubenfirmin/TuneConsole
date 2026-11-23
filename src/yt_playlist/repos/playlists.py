"""PlaylistRepo: playlists, their track membership/ordering, groups, and hidden flags."""
import json

from yt_playlist.core.playlist_scopes import ensure,resolve
from yt_playlist.core import playlist_cleanup_state, playlist_scope_state as scope_state, track_sources
from yt_playlist.core.action_errors import ActionInputError

from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read
from yt_playlist.repos.models import Playlist


_FIRST_TRACK_THUMBNAIL = (
    "(SELECT t.thumbnail FROM playlist_tracks pt JOIN catalog_tracks t ON t.id=pt.track_id "
    "WHERE pt.playlist_id=p.id AND NULLIF(t.thumbnail, '') IS NOT NULL "
    "ORDER BY pt.position, pt.track_id LIMIT 1)"
)


class PlaylistRepo(Repo):
    @snapshot_read
    def cleanup_attempts(self):
        return playlist_cleanup_state.states(self.conn)

    @snapshot_read
    def cleanup_retry_deadlines(self):
        return playlist_cleanup_state.deadlines(self.conn)

    @snapshot_read
    def cleanup_attempt(self, account_id, remote):
        return playlist_cleanup_state.read(self.conn, account_id, remote)

    @synchronized
    def set_cleanup_attempt(self, account_id, remote, state):
        with self.conn.atomic():
            playlist_cleanup_state.save(self.conn, account_id, remote, state)

    @synchronized
    def playlist_reference(self, ytm, account_id=None):
        return resolve(self.conn,ytm,account_id)

    @synchronized
    def playlist_provenance(self, ytm, *, account_id=None) -> dict:
        return scope_state.provenance(self.conn, resolve(self.conn, ytm, account_id))

    @synchronized
    def set_playlist_provenance(self, ytm, *, origin, adopted, description=None, account_id=None) -> None:
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            scope = ensure(self.conn, ytm, account_id)
            scope_state.set_provenance(self.conn, scope, origin=origin, adopted=adopted, description=description)

    @synchronized
    def adopt_generated_playlist(self, ytm, description) -> None:
        with self.conn.atomic():
            scope_state.adopt(self.conn, ensure(self.conn, ytm), description)

    @synchronized
    def is_quarantined_playlist(self, ytm, *, account_id=None) -> bool:
        ytm = resolve(self.conn,ytm,account_id)
        return self.conn.execute('SELECT 1 FROM quarantined_playlists WHERE playlist_scope_id=?', (ytm,)).fetchone() is not None

    @synchronized
    def playlist_video_types(self, playlist_id):
        return [row[0] for row in self.conn.execute(
            "SELECT t.video_type FROM playlist_tracks pt JOIN catalog_tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=?", (playlist_id,))]

    @synchronized
    def upsert_playlist(self, account_id, provider_playlist_id, title, track_count, content_hash, now,
                        thumbnail=None, *, created_at=None) -> int:
        """Sync metadata, preserving the actual creation date when one is known."""
        with self.conn:
            return self._upsert_playlist(account_id, provider_playlist_id, title, track_count,
                                         content_hash, now, thumbnail, created_at=created_at)

    def _upsert_playlist(self, account_id, provider_playlist_id, title, track_count, content_hash, now,
                         thumbnail=None, *, created_at=None, scope=None, current=None):
        """Caller owns the transaction; never commit a partial snapshot."""
        ref = ensure(self.conn, provider_playlist_id, account_id) if scope is None else scope
        if scope is not None and ref != resolve(self.conn, provider_playlist_id, account_id):
            raise ValueError('Playlist scope does not match its account and provider')
        row = current if scope is not None else self.conn.execute(
            "SELECT id,content_hash,last_changed FROM main.playlists "
            "WHERE account_id=? AND playlist_scope_id=? AND is_current=1", (account_id, ref)).fetchone()
        if row is None:
            return self.conn.execute(
                'INSERT INTO playlists(account_id,title,track_count,'
                'content_hash,first_seen,last_seen,last_changed,thumbnail,created_at,created_at_source,playlist_scope_id) '
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (account_id, title, track_count, content_hash, now, now, now, thumbnail,
                 created_at, "local" if created_at is not None else None, ref)).fetchone()[0]
        last_changed = now if row["content_hash"] != content_hash else row["last_changed"]
        self.conn.execute(
            "UPDATE main.playlists SET title=?, track_count=?, content_hash=?, last_seen=?, last_changed=?, "
            "thumbnail=COALESCE(NULLIF(?, ''), thumbnail), created_at=COALESCE(created_at, ?), "
            "created_at_source=COALESCE(created_at_source, ?) WHERE id=? AND is_current=1",
            (title, track_count, content_hash, now, last_changed, thumbnail, created_at,
             "local" if created_at is not None else None, row["id"]))
        return row["id"]

    @lookup_read
    def count_title_prefix(self, prefix):
        return self.conn.execute('SELECT COUNT(*) FROM active_playlists WHERE substr(title,1,length(?))=?',
                                 (prefix, prefix)).fetchone()[0]

    @synchronized
    def publish_radio_destination(self, account, remote, title, group, videos, digest, thumbnail, description, now):
        """Install the initial mirror inside the caller's publication transaction."""
        if not self.conn.in_transaction:
            raise RuntimeError('Radio publication requires a transaction')
        scope = resolve(self.conn, remote, account)
        if scope != remote or remote in ('LM', 'SE') or remote.startswith('@'):
            raise ValueError('Expected a newly created provider playlist')
        self.conn.execute(
            "INSERT INTO playlist_scopes(id,provider_playlist_id,kind,origin,adopted,provenance_description,group_name) "
            "VALUES (?,?,'shared','tuneconsole',0,?,?) ON CONFLICT(id) DO UPDATE SET origin='tuneconsole', "
            "adopted=CASE WHEN playlist_scopes.origin='tuneconsole' THEN playlist_scopes.adopted ELSE 0 END, "
            "provenance_description=COALESCE(playlist_scopes.provenance_description,excluded.provenance_description), "
            "group_name=COALESCE(playlist_scopes.group_name,excluded.group_name)",
            (scope, remote, description, group))
        current = self.conn.execute(
            'SELECT id,content_hash,last_changed FROM active_playlists WHERE account_id=? AND provider_playlist_id=?',
            (account, remote)).fetchone()
        pid = self._upsert_playlist(account, remote, title, len(videos), digest, now, thumbnail, created_at=now,
                                    scope=scope, current=current)
        if current is not None:
            self.conn.execute('DELETE FROM playlist_items WHERE playlist_id=?', (pid,))
        self._insert_video_items(pid, videos)
        return pid, scope

    def _insert_video_items(self, playlist_id, videos, start=0):
        self.conn.execute('''INSERT INTO playlist_items(playlist_id,track_source_id,position)
            SELECT ?,(SELECT id FROM track_sources WHERE video_id=v.value),?+v.key
            FROM json_each(?) v''', (playlist_id, start, json.dumps(videos)))

    def _playlist_videos(self, playlist_id):
        return [row[0] for row in self.conn.execute(
            'SELECT s.video_id FROM playlist_items p JOIN track_sources s ON s.id=p.track_source_id '
            'WHERE p.playlist_id=? ORDER BY p.position', (playlist_id,))]

    @synchronized
    def set_playlist_tracks(self, playlist_id, track_ids) -> None:
        with self.conn:
            self._set_playlist_tracks(playlist_id, track_ids)

    def _set_playlist_tracks(self, playlist_id, track_ids):
        if not self.conn.in_transaction:
            self.conn.execute('BEGIN IMMEDIATE')
        # de-dupe by track id, keeping first position: YouTube's get_playlist can return the same
        # video many times (pagination duplication), which would otherwise inflate the playlist.
        unique = list(dict.fromkeys(track_ids))
        current = [(row[0], row[1]) for row in self.conn.execute(
            'SELECT track_source_id,position FROM playlist_items WHERE playlist_id=? ORDER BY position',
            (playlist_id,))]
        if current == [(track_id, position) for position, track_id in enumerate(unique)]:
            return
        source_ids = json.dumps(unique)
        if self.conn.execute('SELECT count(*) FROM json_each(?) item JOIN track_sources s ON item.value=s.id',
                             (source_ids,)).fetchone()[0] != len(unique):
            raise ValueError('Unknown track reference')
        self.conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
        self.conn.execute('INSERT INTO playlist_items(playlist_id,track_source_id,position) '
                          'SELECT ?,value,key FROM json_each(?)', (playlist_id, source_ids))

    @synchronized
    def playlist_revision(self, account_id, provider_playlist_id):
        """Capture before remote work; retained tokens also detect delete/recreate."""
        ref = resolve(self.conn, provider_playlist_id, account_id)
        row = self.conn.execute('SELECT json_extract(library_revisions,?) FROM playlist_scopes WHERE id=?',
                                (f'$."{int(account_id)}"', ref)).fetchone()
        return row[0] if row and row[0] is not None else 0

    @synchronized
    def playlist_revisions(self, account_id):
        """Include tombstones before a library listing supplies remote titles/absence."""
        return {r['playlist_scope_id']: r['revision'] for r in self.conn.execute(
            'SELECT p.id AS playlist_scope_id,r.value AS revision FROM playlist_scopes p, '
            'json_each(p.library_revisions) r WHERE r.key=?', (str(int(account_id)),))}

    @synchronized
    def publish_playlist_snapshot(self, account_id, provider_playlist_id, title, track_ids, now, *,
                                  expected_revision, digest, thumbnail=None, created_at=None):
        """Publish complete remote metadata/membership iff its pre-fetch owner survives.

        Catalog observations may be retained before this call, but acquire library
        membership only on acceptance. Network I/O must remain outside this transaction.
        A rejected read returns None and cannot advance freshness or placement work.
        """
        with self.conn.atomic():
            if self.playlist_revision(account_id, provider_playlist_id) != expected_revision:
                return None
            ids = list(track_ids)
            pid = self._upsert_playlist(account_id, provider_playlist_id, title, len(ids), digest,
                                        now, thumbnail, created_at=created_at)
            self._set_playlist_tracks(pid, ids)
            track_sources.save_to_library(self.conn, ids, now)
            return pid

    @synchronized
    def publish_confirmed(self, playlist_id, title, videos, digest, entry_ids, now, *,
                          expected_prefix=None, destination=None, empty_digest):
        """Conditionally replace playlist rows using already admitted catalog entries."""
        prefix = list(expected_prefix) if expected_prefix is not None else None
        with self.conn.atomic():
            if playlist_id is None:
                if destination is None:
                    raise ValueError("A destination account and playlist are required")
                account_id, remote_id = destination
                row = self.conn.execute(
                    'SELECT id FROM active_playlists WHERE account_id=? AND provider_playlist_id=?',
                    (account_id, remote_id)).fetchone()
                playlist_id = row[0] if row else self._upsert_playlist(
                    account_id, remote_id, title, 0, empty_digest, now)
            if not self.conn.execute("SELECT 1 FROM active_playlists WHERE id=?", (playlist_id,)).fetchone():
                raise ValueError("Unknown playlist")
            saved = self._playlist_videos(playlist_id)
            incremental = (prefix is not None
                           and saved == prefix
                           and videos[:len(saved)] == prefix
                           and None not in saved)
            start = len(saved) if incremental else 0
            if not incremental:
                self.conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
            self._insert_video_items(playlist_id, videos[start:], start)
            track_sources.save_to_library(self.conn, entry_ids, now)
            self.conn.execute(
                "UPDATE main.playlists SET title=?,track_count=?,content_hash=?,last_seen=?,"
                "last_changed=CASE WHEN content_hash<>? THEN ? ELSE last_changed END WHERE id=?",
                (title, len(videos), digest, now, digest, now, playlist_id))
            self.conn.execute(
                f"UPDATE main.playlists AS p SET thumbnail={_FIRST_TRACK_THUMBNAIL} "
                "WHERE p.id=? AND NULLIF(p.thumbnail, '') IS NULL", (playlist_id,))
        return playlist_id

    @synchronized
    def liked_membership_target(self, account_id, provider_playlist_id, video_id):
        lm = self.conn.execute(
            "SELECT id FROM active_playlists WHERE account_id=? AND provider_playlist_id=?", (account_id,provider_playlist_id)).fetchone()
        row = self.conn.execute(
            "SELECT id FROM track_sources WHERE video_id=? AND library_origin IS NOT NULL", (video_id,)).fetchone()
        if lm is None or row is None:
            return None
        return lm['id']

    @synchronized
    def set_liked_membership(self, lm_id, video_id, on):
        with self.conn.atomic(join=True):
            if on:
                self.conn.execute(
                    "INSERT INTO playlist_items(playlist_id,track_source_id,position) "
                    "SELECT ?,(SELECT id FROM track_sources WHERE video_id=?),"
                    "(SELECT IFNULL(MAX(position),-1)+1 FROM playlist_items WHERE playlist_id=?) "
                    "ON CONFLICT(playlist_id,track_source_id) DO NOTHING",
                    (lm_id, video_id, lm_id))
            else:
                self.conn.execute(
                    "DELETE FROM playlist_items WHERE playlist_id=? "
                    "AND track_source_id=(SELECT id FROM track_sources WHERE video_id=?)", (lm_id, video_id))

    @snapshot_read
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
            "JOIN catalog_tracks t ON t.id=pt.track_id WHERE t.video_id=?", (video_id,)).fetchall()
        return {r["playlist_id"] for r in rows}

    @synchronized
    def set_playlist_title(self, playlist_id, title, now) -> None:
        self.conn.execute("UPDATE main.playlists SET title=?, last_changed=? WHERE id=? AND is_current=1",
                          (title, now, playlist_id))
        self.conn.commit()

    @synchronized
    def set_playlist_track_count(self, playlist_id, count, now) -> None:
        with self.conn:
            self.conn.execute("UPDATE main.playlists SET track_count=?, last_changed=?, last_seen=? WHERE id=? AND is_current=1",
                              (count, now, now, playlist_id))

    @synchronized
    def remove_playlist(self, playlist_id, *, require_empty=False, expected_revision=None):
        """Remove this account membership. Durable scope and curation survive reappearance."""
        with self.conn:
            if expected_revision is not None:
                self.conn.execute('BEGIN IMMEDIATE')
                owner = self.conn.execute('SELECT account_id,provider_playlist_id FROM active_playlists WHERE id=?',
                                          (playlist_id,)).fetchone()
                if owner is None or self.playlist_revision(*owner) != expected_revision:
                    return False
            if require_empty:
                playlist = self.conn.execute(
                    "SELECT track_count FROM active_playlists WHERE id=?", (playlist_id,)).fetchone()
                if playlist is None:
                    raise ActionInputError('playlist_missing')
                if playlist[0] != 0 or self.conn.execute(
                        "SELECT 1 FROM playlist_tracks WHERE playlist_id=? LIMIT 1",
                        (playlist_id,)).fetchone():
                    raise ActionInputError('playlist_cached_tracks')
            self.conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
            self.conn.execute("UPDATE main.playlists SET is_current=0 WHERE id=? AND is_current=1", (playlist_id,))
            return True

    @synchronized
    def get_playlists(self) -> list[Playlist]:
        rows = self.conn.execute("SELECT * FROM active_playlists").fetchall()
        return [Playlist(r["id"], r["account_id"], r["provider_playlist_id"], r["title"],
                         r["track_count"], r["content_hash"], r["first_seen"],
                         r["last_seen"], r["last_changed"], r["thumbnail"], r["created_at"],
                         r["created_at_source"]) for r in rows]

    @synchronized
    def get_playlist_thumbnails(self) -> dict[int, str | None]:
        """Covers for the playlist list, falling back to the first song with artwork."""
        rows = self.conn.execute(
            f"SELECT p.id, COALESCE(NULLIF(p.thumbnail, ''), {_FIRST_TRACK_THUMBNAIL}) "
            "AS thumbnail FROM active_playlists p"
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
                f"UPDATE main.playlists AS p SET thumbnail={_FIRST_TRACK_THUMBNAIL} "
                "WHERE p.id=? AND p.is_current=1 AND NULLIF(p.thumbnail, '') IS NULL", (playlist_id,))

    @snapshot_read
    def home_preview(self, playlist_id):
        """Current metadata and all current members in playlist order for Home."""
        row = self.conn.execute('SELECT * FROM active_playlists WHERE id=?', (playlist_id,)).fetchone()
        if (not row or not row['track_count'] or self.is_quarantined_playlist(row['playlist_scope_id'])):
            return None
        tracks = [dict(r) for r in self.conn.execute(
            'SELECT t.identity_key key,t.title,t.artist,t.video_id,t.thumbnail FROM playlist_tracks p '
            'JOIN catalog_tracks t ON t.id=p.track_id WHERE p.playlist_id=? '
            'ORDER BY p.position', (playlist_id,))]
        if not tracks:
            return None
        return {'id': row['id'], 'ytm': row['provider_playlist_id'], 'title': row['title'],
                'track_count': row['track_count'], 'thumbnail': row['thumbnail'], 'tracks': tracks}

    @lookup_read
    def get_playlist(self, playlist_id) -> Playlist | None:
        row = self.conn.execute("SELECT * FROM active_playlists WHERE id=?", (playlist_id,)).fetchone()
        return None if row is None else Playlist(
            row["id"], row["account_id"], row["provider_playlist_id"], row["title"],
            row["track_count"], row["content_hash"], row["first_seen"],
            row["last_seen"], row["last_changed"], row["thumbnail"], row["created_at"], row["created_at_source"])

    @synchronized
    def get_playlist_for_account(self, account_id, provider_playlist_id) -> Playlist | None:
        """Resolve a remote target afresh after sync may have replaced its local row."""
        row = self.conn.execute(
            "SELECT id FROM active_playlists WHERE account_id=? AND provider_playlist_id=?",
            (account_id, provider_playlist_id)).fetchone()
        return self.get_playlist(row[0]) if row else None

    @synchronized
    def get_playlist_track_keys(self, playlist_id) -> set[str]:
        rows = self.conn.execute(
            "SELECT t.identity_key FROM playlist_tracks pt JOIN catalog_tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=?", (playlist_id,)).fetchall()
        return {r["identity_key"] for r in rows}

    @snapshot_read
    def cleanup_membership_facts(self, excluded_group):
        """Bulk membership read for Cleanup, independent of pair-analysis policy.

        Count unique recordings as get_playlist_track_keys does. Kinds still use
        every upload, including multiple uploads of the same recording.
        """
        playlists = self.get_playlists()
        keys = {p.id: set() for p in playlists}
        types = {p.id: set() for p in playlists}
        for row in self.conn.execute(
                'SELECT pt.playlist_id,t.identity_key,t.video_type FROM playlist_tracks pt '
                'JOIN catalog_tracks t ON t.id=pt.track_id').fetchall():
            keys[row['playlist_id']].add(row['identity_key'])
            if row['video_type'] is not None:
                types[row['playlist_id']].add(row['video_type'])
        excluded = {r[0] for r in self.conn.execute(
            'SELECT p.id FROM active_playlists p JOIN quarantined_playlists q ON q.playlist_scope_id=p.playlist_scope_id '
            'WHERE q.name=?', (excluded_group,))}
        return {'playlists': playlists, 'keys': keys, 'types': types, 'excluded': excluded}

    @snapshot_read
    def snapshot_track_facts(self, track_ids):
        result = []
        for entry in track_ids:
            row = self.conn.execute('SELECT id,identity_key FROM catalog_tracks WHERE id=?', (entry,)).fetchone()
            if row is None:
                raise ValueError('Unknown track reference')
            result.append(dict(row))
        return result

    @snapshot_read
    def confirmed_facts(self, playlist_id, destination=None):
        if playlist_id is None and destination is not None:
            row = self.conn.execute('SELECT id FROM active_playlists WHERE account_id=? AND provider_playlist_id=?', destination).fetchone()
            playlist_id = row[0] if row else None
            if playlist_id is None:
                return {'id': None, 'videos': [], 'unresolved': False}
        if not self.conn.execute('SELECT 1 FROM active_playlists WHERE id=?', (playlist_id,)).fetchone():
            return None
        videos = self._playlist_videos(playlist_id)
        return {'id': playlist_id, 'videos': [video for video in videos if video is not None],
                'unresolved': None in videos}

    @synchronized
    def get_playlist_tracks_with_meta(self, playlist_id) -> list[tuple]:
        rows = self.conn.execute(
            "SELECT t.identity_key, t.video_id, t.title, t.artist, t.duration_s, t.available "
            "FROM playlist_tracks pt JOIN catalog_tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? ORDER BY pt.position", (playlist_id,)).fetchall()
        return [(r["identity_key"], r["video_id"], r["title"], r["artist"], r["duration_s"], r["available"])
                for r in rows]

    @synchronized
    def set_playlist_group(self, ytm, name, *, account_id=None) -> None:
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            scope_state.set_group(self.conn, ensure(self.conn, ytm, account_id), name or None)

    @synchronized
    def hide_playlist(self, ytm, *, account_id=None) -> None:
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            scope_state.set_hidden(self.conn, ensure(self.conn, ytm, account_id), True)

    @synchronized
    def unhide_playlist(self, ytm, *, account_id=None) -> None:
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            scope_state.set_hidden(self.conn, ensure(self.conn, ytm, account_id), False)

    @synchronized
    def get_hidden_playlists(self) -> set:
        return scope_state.hidden(self.conn)

    @synchronized
    def get_playlist_groups(self) -> dict:
        return scope_state.groups(self.conn)
