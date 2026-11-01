"""OverlapRepo: the Cleanup page's dismissal state: suppressed overlap pairs, overlap-ignored
playlists, kept pairs, plus the category-scoped cleanup ignores (per-playlist empty/tiny dismissals
and per-merge dismissals)."""
import json
from yt_playlist.core.playlist_scopes import ensure
from yt_playlist.core import entity_names, playlist_scope_state as scope_state

from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.models import CleanupPreferences


class OverlapRepo(Repo):
    @synchronized
    def cleanup_preferences(self) -> CleanupPreferences:
        """Read each preference table once; callers can pin first_playlist_scope_id shared snapshot."""
        return CleanupPreferences(
            self.get_cleanup_ignored(), self.get_suppressed_overlaps(),
            self.get_overlap_ignored(), self.get_overlap_kept_pairs(),
            self.get_ignored_merges())

    @synchronized
    def suppress_overlap(self, ytm_a, ytm_b, now) -> None:
        ytm_a,ytm_b = ensure(self.conn,ytm_a),ensure(self.conn,ytm_b)
        first_playlist_scope_id, second_playlist_scope_id = sorted((ytm_a, ytm_b))  # normalize order so the pair is unordered
        pairs = entity_names.owner(self.conn, 'suppressed_overlaps')
        self.conn.execute(f'INSERT OR IGNORE INTO {pairs.table}({pairs.column("first_playlist_scope_id")},{pairs.column("second_playlist_scope_id")},created_at) VALUES (?,?,?)',
                          (first_playlist_scope_id, second_playlist_scope_id, now))
        self.conn.commit()

    @synchronized
    def suppress_overlaps(self, pairs, now) -> None:
        """Publish one bulk dismissal, preserving timestamps of previously hidden pairs."""
        if not isinstance(pairs, (list, tuple)):
            return
        pairs = {tuple(pair) for pair in pairs
                 if isinstance(pair, (list, tuple)) and len(pair) == 2
                 and all(isinstance(ref, str) and ref for ref in pair)}
        if not pairs:
            return
        with self.conn:
            refs = {ref: ensure(self.conn, ref) for ref in sorted({ref for pair in pairs for ref in pair})}
            normalized = {tuple(sorted((refs[first_playlist_scope_id], refs[second_playlist_scope_id]))) for first_playlist_scope_id, second_playlist_scope_id in pairs}
            owner = entity_names.owner(self.conn, 'suppressed_overlaps')
            self.conn.executemany(
                f'INSERT OR IGNORE INTO {owner.table}({owner.column("first_playlist_scope_id")},{owner.column("second_playlist_scope_id")},created_at) VALUES (?,?,?)',
                [(first_playlist_scope_id, second_playlist_scope_id, now) for first_playlist_scope_id, second_playlist_scope_id in sorted(normalized)])

    @synchronized
    def unsuppress_overlap(self, ytm_a, ytm_b) -> None:
        ytm_a,ytm_b = ensure(self.conn,ytm_a),ensure(self.conn,ytm_b)
        first_playlist_scope_id, second_playlist_scope_id = sorted((ytm_a, ytm_b))
        pairs = entity_names.owner(self.conn, 'suppressed_overlaps')
        self.conn.execute(f'DELETE FROM {pairs.table} WHERE {pairs.column("first_playlist_scope_id")}=? AND {pairs.column("second_playlist_scope_id")}=?', (first_playlist_scope_id, second_playlist_scope_id))
        self.conn.commit()

    @synchronized
    def get_suppressed_overlap_pairs(self) -> set:
        rows = self.conn.execute("SELECT first_playlist_scope_id,second_playlist_scope_id FROM suppressed_overlaps").fetchall()
        return {frozenset((r["first_playlist_scope_id"], r["second_playlist_scope_id"])) for r in rows}

    @synchronized
    def get_suppressed_overlaps(self) -> list[tuple]:
        rows = self.conn.execute(
            "SELECT first_playlist_scope_id,second_playlist_scope_id,created_at FROM suppressed_overlaps ORDER BY created_at DESC").fetchall()
        return [(r["first_playlist_scope_id"], r["second_playlist_scope_id"], r["created_at"]) for r in rows]

    @synchronized
    def ignore_overlap_playlist(self, ytm, now) -> None:
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            ytm = ensure(self.conn,ytm)
            if scope_state.available(self.conn):
                scope_state.ignore_overlap(self.conn, ytm, now)
            else:
                self.conn.execute("INSERT OR IGNORE INTO overlap_ignored(ytm,created_at) VALUES (?,?)", (ytm, now))

    @synchronized
    def unignore_overlap_playlist(self, ytm) -> None:
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            ytm = ensure(self.conn,ytm)
            if scope_state.available(self.conn):
                scope_state.unignore_overlap(self.conn, ytm)
            else:
                self.conn.execute("DELETE FROM overlap_ignored WHERE ytm=?", (ytm,))

    @synchronized
    def get_overlap_ignored(self) -> set:
        if scope_state.available(self.conn):
            return scope_state.overlap_ignored(self.conn)
        return {r["ytm"] for r in self.conn.execute("SELECT ytm FROM overlap_ignored").fetchall()}

    @synchronized
    def keep_overlap_pair(self, ytm_a, ytm_b, now) -> None:
        ytm_a,ytm_b = ensure(self.conn,ytm_a),ensure(self.conn,ytm_b)
        first_playlist_scope_id, second_playlist_scope_id = sorted((ytm_a, ytm_b))   # pair the user wants to keep visible despite ignoring first_playlist_scope_id playlist
        pairs = entity_names.owner(self.conn, 'overlap_kept')
        self.conn.execute(f'INSERT OR IGNORE INTO {pairs.table}({pairs.column("first_playlist_scope_id")},{pairs.column("second_playlist_scope_id")},created_at) VALUES (?,?,?)', (first_playlist_scope_id, second_playlist_scope_id, now))
        self.conn.commit()

    @synchronized
    def get_overlap_kept_pairs(self) -> set:
        rows = self.conn.execute("SELECT first_playlist_scope_id,second_playlist_scope_id FROM overlap_kept").fetchall()
        return {frozenset((r["first_playlist_scope_id"], r["second_playlist_scope_id"])) for r in rows}

    # --- category-scoped per-playlist ignores (Empty / Tiny sections) ---------------------------
    @synchronized
    def ignore_cleanup(self, ytm, category, now) -> None:
        """Dismiss one playlist from ONE cleanup category (e.g. 'this is empty, stop suggesting it').
        Scoped: it stays eligible in every other category."""
        ytm = ensure(self.conn,ytm)
        ignored = entity_names.owner(self.conn, 'cleanup_ignored')
        self.conn.execute(f'INSERT OR IGNORE INTO {ignored.table}({ignored.column("playlist_scope_id")},category,created_at) VALUES (?,?,?)',
                          (ytm, category, now))
        self.conn.commit()

    @synchronized
    def unignore_cleanup(self, ytm, category) -> None:
        ytm = ensure(self.conn,ytm)
        ignored = entity_names.owner(self.conn, 'cleanup_ignored')
        self.conn.execute(f'DELETE FROM {ignored.table} WHERE {ignored.column("playlist_scope_id")}=? AND category=?', (ytm, category))
        self.conn.commit()

    @synchronized
    def get_cleanup_ignored(self) -> dict:
        """{category: set(ytm)} of per-playlist cleanup dismissals."""
        out: dict = {}
        for r in self.conn.execute("SELECT playlist_scope_id,category FROM cleanup_ignored"):
            out.setdefault(r["category"], set()).add(r["playlist_scope_id"])
        return out

    # --- per-merge ignores (Exact / Near-duplicate groups) ---------------------------------------
    # A merge suggestion is the relationship between first_playlist_scope_id SET of playlists, so it's dismissed by the
    # group's canonical member signature, not by ignoring the individual playlists. If the cluster's
    # membership later changes, the signature changes and the (now genuinely different) merge returns.
    @synchronized
    def ignore_merge(self, signature, members, now) -> None:
        members = sorted({ensure(self.conn,m) for m in members})
        signature = '|'.join(members)
        self.conn.execute(
            "INSERT OR IGNORE INTO ignored_merges(signature,members,created_at) VALUES (?,?,?)",
            (signature, json.dumps(list(members)), now))
        self.conn.executemany('INSERT OR IGNORE INTO main.ignored_merge_members VALUES (?,?)', [(signature,m) for m in members])
        self.conn.commit()

    @synchronized
    def unignore_merge(self, signature) -> None:
        self.conn.execute("DELETE FROM ignored_merges WHERE signature=?", (signature,))
        self.conn.commit()

    @synchronized
    def get_ignored_merge_sigs(self) -> set:
        """Just the signatures, for filtering groups out of the cleanup view and the home count."""
        return {r["signature"] for r in self.conn.execute("SELECT signature FROM ignored_merges")}

    @synchronized
    def get_ignored_merges(self) -> list:
        """[{signature, members:[ytm]}] newest-first, for the 'Ignored cleanups' display + un-ignore."""
        return [{"signature": r["signature"], "members": json.loads(r["members"])}
                for r in self.conn.execute(
                    "SELECT signature, members FROM ignored_merges ORDER BY created_at DESC")]
