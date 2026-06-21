"""HistoryRepo — listening-history snapshots and their item keys."""
from yt_playlist.repos.base import Repo, synchronized


class HistoryRepo(Repo):
    @synchronized
    def add_history_snapshot(self, identity_id, taken_at, item_keys) -> int:
        cur = self.conn.execute(
            "INSERT INTO history_snapshots(identity_id,taken_at) VALUES (?,?)",
            (identity_id, taken_at))
        sid = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO history_items(snapshot_id,identity_key) VALUES (?,?)",
            [(sid, k) for k in item_keys])
        self.conn.commit()
        return sid

    @synchronized
    def get_recent_history_keys(self, since_ts) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT hi.identity_key FROM history_items hi "
            "JOIN history_snapshots hs ON hs.id=hi.snapshot_id WHERE hs.taken_at>=?",
            (since_ts,)).fetchall()
        return {r["identity_key"] for r in rows}

    @synchronized
    def recent_keys_ordered(self, since_ts, limit=None) -> list[str]:
        """Identity keys played at/after since_ts, MOST-RECENT first (deduped by latest snapshot).
        For the recent-mood centroid, which wants the latest plays — not an arbitrary subset of a set."""
        rows = self.conn.execute(
            "SELECT hi.identity_key k, MAX(hs.taken_at) last FROM history_items hi "
            "JOIN history_snapshots hs ON hs.id=hi.snapshot_id WHERE hs.taken_at>=? "
            "GROUP BY hi.identity_key ORDER BY last DESC", (since_ts,)).fetchall()
        keys = [r["k"] for r in rows]
        return keys[:limit] if limit else keys
