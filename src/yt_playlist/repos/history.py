"""HistoryRepo: listening-history snapshots and their item keys."""
from yt_playlist.repos.base import Repo, synchronized

# A truncated get_history() reply (the app sees occasional 1-3 item windows) must NOT replace a full
# cached window, or the dropped tracks would re-count as "plays" when the full window returns. So a
# window smaller than this fraction of the cached one is treated as a hiccup and ignored (#49).
_WINDOW_MIN_FRACTION = 0.5


class HistoryRepo(Repo):
    @synchronized
    def add_history_snapshot(self, identity_id, taken_at, item_keys) -> int:
        """Record a raw snapshot: every key becomes a history_item. The plays model uses
        record_history_plays (which dedups); this stays raw for tests and explicit callers."""
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
    def record_history_plays(self, identity_id, taken_at, item_keys) -> int:
        """#49 Capture-time play detection. YouTube's recently-played window is re-fetched every sync
        and ~91% overlaps the previous one, so storing it whole made COUNT(*) count lingering, not
        plays. Instead, diff the new window against this identity's cached previous window and record
        ONLY the newly-appeared keys as a snapshot (so all the COUNT(*) read queries stay correct and
        cheap). Returns the number of NEW plays recorded.

        Robust to truncated replies: a window much smaller than the cached one is treated as a hiccup
        (no plays recorded, cache preserved) so an API blip can't drop the window and re-count later."""
        incoming = {k for k in item_keys if k}
        if not incoming:
            return 0
        prev = self._history_window_keys(identity_id)
        if prev and len(incoming) < len(prev) * _WINDOW_MIN_FRACTION:
            return 0                                       # truncated/hiccup: ignore, keep the cache
        new = incoming - prev
        if new:
            self.add_history_snapshot(identity_id, taken_at, sorted(new))
        self._replace_history_window(identity_id, incoming)
        return len(new)

    def _history_window_keys(self, identity_id) -> set:
        return {r["identity_key"] for r in self.conn.execute(
            "SELECT identity_key FROM history_window WHERE identity_id=?", (identity_id,))}

    def _replace_history_window(self, identity_id, keys) -> None:
        self.conn.execute("DELETE FROM history_window WHERE identity_id=?", (identity_id,))
        self.conn.executemany("INSERT INTO history_window(identity_id, identity_key) VALUES (?,?)",
                              [(identity_id, k) for k in keys])
        self.conn.commit()

    @synchronized
    def get_recent_history_keys(self, since_ts) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT hi.identity_key FROM history_items hi "
            "JOIN history_snapshots hs ON hs.id=hi.snapshot_id WHERE hs.taken_at>=?",
            (since_ts,)).fetchall()
        return {r["identity_key"] for r in rows}

    @synchronized
    def history_keys_before(self, ts) -> set[str]:
        """Distinct identity keys played strictly before `ts` (the temporal-split context set). The
        complement of get_recent_history_keys, used by eval to hold out the most recent window."""
        rows = self.conn.execute(
            "SELECT DISTINCT hi.identity_key FROM history_items hi "
            "JOIN history_snapshots hs ON hs.id=hi.snapshot_id WHERE hs.taken_at<?",
            (ts,)).fetchall()
        return {r["identity_key"] for r in rows}

    @synchronized
    def recent_play_counts(self, limit) -> dict:
        """{identity_key: play count} over the most recent `limit` play events (one row per history
        item, newest-first). Frequency-weighted recent listening - repeats count - for the taste page's
        'recent mix vs usual' deviation. A very large `limit` yields the all-time play-count basis, so
        both sides of the deviation can be computed the same way."""
        rows = self.conn.execute(
            "SELECT identity_key k, COUNT(*) c FROM ("
            "  SELECT hi.identity_key FROM history_items hi "
            "  JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "  ORDER BY hs.taken_at DESC, hi.rowid DESC LIMIT ?"
            ") GROUP BY identity_key", (limit,)).fetchall()
        return {r["k"]: r["c"] for r in rows}

    @synchronized
    def recent_keys_ordered(self, since_ts, limit=None) -> list[str]:
        """Identity keys played at/after since_ts, MOST-RECENT first (deduped by latest snapshot).
        For the recent-mood centroid, which wants the latest plays, not an arbitrary subset of a set."""
        rows = self.conn.execute(
            "SELECT hi.identity_key k, MAX(hs.taken_at) last FROM history_items hi "
            "JOIN history_snapshots hs ON hs.id=hi.snapshot_id WHERE hs.taken_at>=? "
            "GROUP BY hi.identity_key ORDER BY last DESC", (since_ts,)).fetchall()
        keys = [r["k"] for r in rows]
        return keys[:limit] if limit else keys
