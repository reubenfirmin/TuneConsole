"""OverlapRepo — the Cleanup page's overlap state: suppressed pairs, ignored playlists, kept pairs."""
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
