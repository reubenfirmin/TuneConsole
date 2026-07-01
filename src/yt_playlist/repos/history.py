"""HistoryRepo: listening-history snapshots and their item keys."""
import datetime

from yt_playlist.repos.base import Repo, synchronized

_NOON = 43200            # store each play-date snapshot at noon UTC, so taken_at queries are day-stable
_EPOCH = datetime.date(1970, 1, 1)


def _parse_played_date(played, taken_at) -> float:
    """Resolve YouTube's relative `played` bucket ('Today' / 'Yesterday' / 'Jun 25' / 'Jun 25, 2025') to
    an ABSOLUTE UTC day (a noon timestamp), anchored on the sync time `taken_at` (#58). Because the bucket
    is relative, the SAME play reads 'Today' on one sync and 'Yesterday' the next -- both resolve to the
    same date, which is what makes play recording idempotent. Unknown/localized labels fall back to the
    sync day (so a play is at worst attributed to the day we observed it, never duplicated)."""
    sync_day = datetime.datetime.fromtimestamp(taken_at, tz=datetime.timezone.utc).date()
    p = (played or "").strip().lower()
    if p in ("", "today"):
        day = sync_day
    elif p == "yesterday":
        day = sync_day - datetime.timedelta(days=1)
    else:
        day = sync_day
        s = (played or "").strip()
        for fmt, has_year in (("%b %d, %Y", True), ("%B %d, %Y", True), ("%b %d", False), ("%B %d", False)):
            try:
                if has_year:
                    d = datetime.datetime.strptime(s, fmt).date()
                else:                                    # pin the sync year explicitly (no 1900 default)
                    d = datetime.datetime.strptime(f"{s} {sync_day.year}", f"{fmt} %Y").date()
                    if d > sync_day:                     # e.g. "Dec 30" seen in early Jan -> previous year
                        d = d.replace(year=sync_day.year - 1)
            except (ValueError, TypeError, AttributeError):
                continue
            day = d
            break
    return (day - _EPOCH).days * 86400 + _NOON


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
    def record_history_plays(self, identity_id, taken_at, items) -> int:
        """#49/#58 Idempotent capture-time play recording. `items` is the recently-played window as
        [(identity_key, played_bucket), ...] (a bare key is also accepted -> played=None -> the sync
        day). Each play is keyed by (identity_key, played-DATE): the relative `played` bucket resolved
        to an absolute day via taken_at. Re-fetching the same window -- even when 'Today' becomes
        'Yesterday' the next day -- maps to the same date and records nothing new, so COUNT(*) counts
        plays, not lingering; a reordered/unstable window cannot create phantom plays. Same-date repeats
        merge. Returns the number of new (key, date) plays recorded.

        play_count(key) = COUNT(*) over history_items = number of distinct days the track was played.
        """
        by_date: dict = {}
        for item in items:
            key, played = (item, None) if isinstance(item, str) else item
            if key:
                by_date.setdefault(_parse_played_date(played, taken_at), set()).add(key)
        recorded = 0
        for play_ts, keys in by_date.items():
            sid = self._snapshot_for_date(identity_id, play_ts)
            existing = {r["identity_key"] for r in self.conn.execute(
                "SELECT identity_key FROM history_items WHERE snapshot_id=?", (sid,))}
            new = sorted(keys - existing)
            if new:
                self.conn.executemany("INSERT INTO history_items(snapshot_id, identity_key) VALUES (?,?)",
                                      [(sid, k) for k in new])
                recorded += len(new)
        self.conn.commit()
        return recorded

    def _snapshot_for_date(self, identity_id, play_ts):
        """Get-or-create the single snapshot for (identity, play-date), so all of a day's plays share
        one snapshot and (snapshot, key) is the de-dup unit."""
        row = self.conn.execute(
            "SELECT id FROM history_snapshots WHERE identity_id=? AND taken_at=?",
            (identity_id, play_ts)).fetchone()
        if row:
            return row["id"]
        return self.conn.execute(
            "INSERT INTO history_snapshots(identity_id, taken_at) VALUES (?,?)",
            (identity_id, play_ts)).lastrowid

    @synchronized
    def reset_play_history(self, identity_id=None) -> None:
        """#58 Clear stored play history so it rebuilds clean from the next sync (the reset a full sync
        can call). Scoped to one identity, or all when None. Also clears the legacy window cache."""
        if identity_id is None:
            self.conn.execute("DELETE FROM history_items")
            self.conn.execute("DELETE FROM history_snapshots")
            self.conn.execute("DELETE FROM history_window")
        else:
            self.conn.execute("DELETE FROM history_items WHERE snapshot_id IN "
                              "(SELECT id FROM history_snapshots WHERE identity_id=?)", (identity_id,))
            self.conn.execute("DELETE FROM history_snapshots WHERE identity_id=?", (identity_id,))
            self.conn.execute("DELETE FROM history_window WHERE identity_id=?", (identity_id,))
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
