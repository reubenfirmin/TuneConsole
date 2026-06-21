"""RecModelRepo — the learned taste model: blend weights, feedback events, and embedding vectors.

Owns the rec_weights / rec_feedback / rec_vectors tables (created in store.py's central SCHEMA).
Split out of the former monolithic RecRepo so each rec concern is its own focused DAO.
"""
from yt_playlist.repos.base import Repo, synchronized


class RecModelRepo(Repo):
    # --- learned blend weights ---
    @synchronized
    def get_weights(self) -> dict:
        """Learned blend weights by axis (missing axis = prior 1.0)."""
        return {r["axis"]: r["weight"] for r in self.conn.execute("SELECT axis, weight FROM rec_weights")}

    @synchronized
    def nudge_weight(self, axis, factor, lo=0.2, hi=3.0) -> float:
        """Multiply an axis weight by factor (clamped), then shrink slightly toward the 1.0 prior."""
        row = self.conn.execute("SELECT weight FROM rec_weights WHERE axis=?", (axis,)).fetchone()
        w = max(lo, min(hi, (row["weight"] if row else 1.0) * factor))
        w = w + (1.0 - w) * 0.05
        self.conn.execute("INSERT INTO rec_weights(axis, weight) VALUES (?, ?) "
                          "ON CONFLICT(axis) DO UPDATE SET weight=excluded.weight", (axis, w))
        self.conn.commit()
        return w

    @synchronized
    def set_weight(self, axis, weight, lo=0.2, hi=3.0) -> None:
        """Manual override (Taste Model page). Clamped to the same [lo, hi] band as nudge_weight so a
        stray 0/negative can't silently disable a for_you lane (its fair-queue ratio would never win)."""
        w = max(lo, min(hi, float(weight)))
        self.conn.execute("INSERT INTO rec_weights(axis, weight) VALUES (?, ?) "
                          "ON CONFLICT(axis) DO UPDATE SET weight=excluded.weight", (axis, w))
        self.conn.commit()

    @synchronized
    def reset_weights(self) -> None:
        self.conn.execute("DELETE FROM rec_weights")
        self.conn.commit()

    # --- feedback events (dismiss / less / more / mute / not_now) ---
    @synchronized
    def record_feedback(self, surface, item_key, kind, reason=None, scope="", until=None, now=None) -> None:
        """Persist a feedback event (dismiss/less/more/mute/not_now). Upserts per (surface,item,scope)."""
        self.conn.execute(
            "INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(surface,item_key,scope) DO UPDATE SET "
            "kind=excluded.kind, reason=excluded.reason, until=excluded.until, created_at=excluded.created_at",
            (surface, item_key, kind, reason, scope or "", until, now))
        self.conn.commit()

    @synchronized
    def suppressed_keys(self, surface, now, scope="") -> set:
        """Track keys to hide on a surface: dismissed/muted/snoozed, honoring any 'until' expiry."""
        rows = self.conn.execute(
            "SELECT item_key FROM rec_feedback WHERE surface=? AND (scope='' OR scope=?) "
            "AND kind IN ('dismiss','mute','not_now') AND (until IS NULL OR until>?)",
            (surface, scope or "", now)).fetchall()
        return {r["item_key"] for r in rows}

    @synchronized
    def muted_artists(self) -> set:
        """Artist names the user has muted (stored as item_key 'artist:<name>')."""
        rows = self.conn.execute("SELECT item_key FROM rec_feedback WHERE kind='mute'").fetchall()
        return {r["item_key"][7:] for r in rows if r["item_key"].startswith("artist:")}

    @synchronized
    def feedback_summary(self) -> dict:
        """{kind: count} of stored feedback events — for the Taste Model page."""
        return {r["kind"]: r["c"] for r in self.conn.execute(
            "SELECT kind, COUNT(*) c FROM rec_feedback GROUP BY kind")}

    @synchronized
    def clear_feedback(self) -> None:
        self.conn.execute("DELETE FROM rec_feedback")
        self.conn.execute("DELETE FROM rec_impressions")
        self.conn.commit()

    # --- taste-embedding vectors ---
    @synchronized
    def replace_rec_vectors(self, rows) -> None:
        """Atomically replace all taste-embedding vectors. rows = iterable of (identity_key, bytes)."""
        self.conn.execute("DELETE FROM rec_vectors")
        self.conn.executemany("INSERT INTO rec_vectors(identity_key, vec) VALUES (?,?)", rows)
        self.conn.commit()

    @synchronized
    def get_rec_vectors(self) -> list[tuple]:
        return [(r["identity_key"], r["vec"])
                for r in self.conn.execute("SELECT identity_key, vec FROM rec_vectors")]

    @synchronized
    def rec_vectors_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM rec_vectors").fetchone()["c"]
