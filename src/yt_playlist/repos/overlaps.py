"""OverlapRepo: the Cleanup page's dismissal state: suppressed overlap pairs, overlap-ignored
playlists, kept pairs, plus the category-scoped cleanup ignores (per-playlist empty/tiny dismissals
and per-merge dismissals)."""
import json

from yt_playlist.repos.base import Repo, synchronized


class OverlapRepo(Repo):
    @synchronized
    def suppress_overlap(self, ytm_a, ytm_b, now) -> None:
        a, b = sorted((ytm_a, ytm_b))  # normalize order so the pair is unordered
        self.conn.execute("INSERT OR IGNORE INTO suppressed_overlaps(a,b,created_at) VALUES (?,?,?)",
                          (a, b, now))
        self.conn.commit()

    @synchronized
    def unsuppress_overlap(self, ytm_a, ytm_b) -> None:
        a, b = sorted((ytm_a, ytm_b))
        self.conn.execute("DELETE FROM suppressed_overlaps WHERE a=? AND b=?", (a, b))
        self.conn.commit()

    @synchronized
    def get_suppressed_overlap_pairs(self) -> set:
        rows = self.conn.execute("SELECT a,b FROM suppressed_overlaps").fetchall()
        return {frozenset((r["a"], r["b"])) for r in rows}

    @synchronized
    def get_suppressed_overlaps(self) -> list[tuple]:
        rows = self.conn.execute(
            "SELECT a,b,created_at FROM suppressed_overlaps ORDER BY created_at DESC").fetchall()
        return [(r["a"], r["b"], r["created_at"]) for r in rows]

    @synchronized
    def ignore_overlap_playlist(self, ytm, now) -> None:
        self.conn.execute("INSERT OR IGNORE INTO overlap_ignored(ytm,created_at) VALUES (?,?)", (ytm, now))
        self.conn.commit()

    @synchronized
    def unignore_overlap_playlist(self, ytm) -> None:
        self.conn.execute("DELETE FROM overlap_ignored WHERE ytm=?", (ytm,))
        self.conn.commit()

    @synchronized
    def get_overlap_ignored(self) -> set:
        return {r["ytm"] for r in self.conn.execute("SELECT ytm FROM overlap_ignored").fetchall()}

    @synchronized
    def keep_overlap_pair(self, ytm_a, ytm_b, now) -> None:
        a, b = sorted((ytm_a, ytm_b))   # pair the user wants to keep visible despite ignoring a playlist
        self.conn.execute("INSERT OR IGNORE INTO overlap_kept(a,b,created_at) VALUES (?,?,?)", (a, b, now))
        self.conn.commit()

    @synchronized
    def get_overlap_kept_pairs(self) -> set:
        rows = self.conn.execute("SELECT a,b FROM overlap_kept").fetchall()
        return {frozenset((r["a"], r["b"])) for r in rows}

    # --- category-scoped per-playlist ignores (Empty / Tiny sections) ---------------------------
    @synchronized
    def ignore_cleanup(self, ytm, category, now) -> None:
        """Dismiss one playlist from ONE cleanup category (e.g. 'this is empty, stop suggesting it').
        Scoped: it stays eligible in every other category."""
        self.conn.execute("INSERT OR IGNORE INTO cleanup_ignored(ytm,category,created_at) VALUES (?,?,?)",
                          (ytm, category, now))
        self.conn.commit()

    @synchronized
    def unignore_cleanup(self, ytm, category) -> None:
        self.conn.execute("DELETE FROM cleanup_ignored WHERE ytm=? AND category=?", (ytm, category))
        self.conn.commit()

    @synchronized
    def get_cleanup_ignored(self) -> dict:
        """{category: set(ytm)} of per-playlist cleanup dismissals."""
        out: dict = {}
        for r in self.conn.execute("SELECT ytm,category FROM cleanup_ignored"):
            out.setdefault(r["category"], set()).add(r["ytm"])
        return out

    # --- per-merge ignores (Exact / Near-duplicate groups) ---------------------------------------
    # A merge suggestion is the relationship between a SET of playlists, so it's dismissed by the
    # group's canonical member signature, not by ignoring the individual playlists. If the cluster's
    # membership later changes, the signature changes and the (now genuinely different) merge returns.
    @synchronized
    def ignore_merge(self, signature, members, now) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO ignored_merges(signature,members,created_at) VALUES (?,?,?)",
            (signature, json.dumps(list(members)), now))
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
