"""Mood feedback, accumulated themes and their graduation audit history."""
import json

from yt_playlist.repos.base import Repo, synchronized

MOOD_EVENT_CAP = 200


class RecSignalsRepo(Repo):
    @synchronized
    def record_mood(self, keys, direction, now, prune_before=None) -> None:
        """Log a mood signal (seed keys + ±direction). Bounded by age (via prune_before, if set)
        and by count (only the newest MOOD_EVENT_CAP rows are kept). When prune_before is set,
        rows with created_at < prune_before are deleted in the same transaction. Read by recent_mood_events."""
        self.conn.execute("INSERT INTO rec_mood(created_at, direction, keys) VALUES (?,?,?)",
                          (now, int(direction), json.dumps(list(keys))))
        if prune_before is not None:
            self.conn.execute("DELETE FROM rec_mood WHERE created_at < ?", (prune_before,))
        self.conn.execute(
            "DELETE FROM rec_mood WHERE rowid NOT IN "
            "(SELECT rowid FROM rec_mood ORDER BY created_at DESC, rowid DESC LIMIT ?)",
            (MOOD_EVENT_CAP,))
        self.conn.commit()

    @synchronized
    def recent_mood_events(self, limit=None) -> list:
        """Recent mood events, newest-first: [(created_at, direction, [keys])]. Persistent, recency is
        the caller's concern (interaction-rank weighting), not a time window."""
        limit = MOOD_EVENT_CAP if limit is None else limit
        return [(r["created_at"], r["direction"], json.loads(r["keys"]))
                for r in self.conn.execute(
                    "SELECT created_at, direction, keys FROM rec_mood "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))]

    def active_mood(self, now, window_h=8) -> list:
        """Deprecated alias for recent_mood_events(), retained for callers not yet updated to the
        persistent API. Time-window args are ignored; all stored events are returned newest-first."""
        return self.recent_mood_events()

    @synchronized
    def bump_theme(self, facet, contribution, now) -> float:
        self.conn.execute(
            "INSERT INTO rec_theme(facet, score, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(facet) DO UPDATE SET score = score + excluded.score, updated_at = excluded.updated_at",
            (facet, contribution, now))
        self.conn.commit()
        return self.conn.execute("SELECT score FROM rec_theme WHERE facet=?", (facet,)).fetchone()["score"]

    @synchronized
    def discount_theme(self, facet, amount) -> None:
        self.conn.execute("UPDATE rec_theme SET score = score - ? WHERE facet=?", (amount, facet))
        self.conn.commit()

    @synchronized
    def get_theme(self, facet):
        row = self.conn.execute("SELECT score FROM rec_theme WHERE facet=?", (facet,)).fetchone()
        return row["score"] if row else None

    @synchronized
    def theme_rows(self) -> list:
        """All graduation-ledger rows {facet, score, updated_at}, strongest-magnitude first - the
        transient->permanent funnel, for the Taste-model transparency view."""
        return self.conn.execute(
            "SELECT facet, score, updated_at FROM rec_theme ORDER BY ABS(score) DESC").fetchall()

    # --- graduation instrumentation: one row per threshold-crossing nudge (§1c model-health) ---
    @synchronized
    def log_graduation(self, axis, source, score, factor, new_weight, now) -> None:
        """Record one graduation event: the axis, the source signal that drove it, the ledger value at
        the crossing, the nudge factor, and the resulting permanent weight. Append-only; read by the
        model-health panel to show graduation counts by source and to tune SOURCE_W_* on evidence."""
        self.conn.execute(
            "INSERT INTO rec_grad_log(axis, source, score, factor, new_weight, created_at) "
            "VALUES (?,?,?,?,?,?)", (axis, source, score, factor, new_weight, now))
        self.conn.commit()

    @synchronized
    def recent_graduations(self, limit=50, *, since=None, until=None) -> list:
        """The most recent graduation events, newest-first: rows of
        {id, axis, source, score, factor, new_weight, created_at}."""
        conditions, args = [], []
        if since is not None:
            conditions.append("created_at>=?")
            args.append(since)
        if until is not None:
            conditions.append("created_at<=?")
            args.append(until)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        return self.conn.execute(
            "SELECT id, axis, source, score, factor, new_weight, created_at FROM rec_grad_log "
            f"{where} ORDER BY created_at DESC, id DESC LIMIT ?", [*args, limit]).fetchall()

    @synchronized
    def graduation_audit_rows(self) -> list:
        """Every graduation log row's (axis, source, factor), oldest-first, for source audits."""
        return self.conn.execute(
            "SELECT axis, source, factor FROM rec_grad_log ORDER BY id").fetchall()

    @synchronized
    def graduation_log_counts(self) -> dict:
        """{source: count} over all logged graduation events, for the model-health panel."""
        return {r["source"]: r["c"] for r in self.conn.execute(
            "SELECT source, COUNT(*) c FROM rec_grad_log GROUP BY source").fetchall()}

    # --- play-exposure graduation: per-axis once-per-UTC-day stamp (the idempotency guard) ---
    @synchronized
    def get_play_graduated_day(self, axis):
        """The UTC day-string the play-exposure funnel last graduated `axis`, or None if never."""
        row = self.conn.execute(
            "SELECT last_graduated_day FROM rec_play_grad WHERE axis=?", (axis,)).fetchone()
        return row["last_graduated_day"] if row else None

    @synchronized
    def set_play_graduated_day(self, axis, day) -> None:
        """Stamp the UTC day the play-exposure funnel last touched `axis` (once-per-day idempotency)."""
        self.conn.execute(
            "INSERT INTO rec_play_grad(axis, last_graduated_day) VALUES (?,?) "
            "ON CONFLICT(axis) DO UPDATE SET last_graduated_day=excluded.last_graduated_day", (axis, day))
        self.conn.commit()
