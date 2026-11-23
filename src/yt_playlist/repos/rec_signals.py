"""Mood feedback, accumulated themes and their graduation audit history."""
from yt_playlist.core import mood, taste_axes
from yt_playlist.repos.base import Repo, synchronized

class RecSignalsRepo(Repo):
    @synchronized
    def record_mood(self, keys, direction, now, *, cap, prune_before=None) -> None:
        """Persist a signal and prune to the caller's explicit bounds atomically."""
        mood.record(self.conn, keys, direction, now, cap=cap, prune_before=prune_before)
        self.conn.commit()

    @synchronized
    def recent_mood_events(self, limit) -> list:
        """Recent mood events, newest-first: [(created_at, direction, [keys])]. Persistent, recency is
        the caller's concern (interaction-rank weighting), not a time window."""
        return mood.recent(self.conn, limit)

    @synchronized
    def bump_theme(self, facet, contribution, now) -> float:
        reference = taste_axes.identity(self.conn, facet, create=True)
        score = self.conn.execute('UPDATE taste_axes SET theme_score=coalesce(theme_score,0)+?,theme_updated_at=? '
                                  'WHERE id=? RETURNING theme_score', (contribution, now, reference)).fetchone()[0]
        self.conn.commit()
        return score

    @synchronized
    def discount_theme(self, facet, amount) -> None:
        self.conn.execute('UPDATE taste_axes SET theme_score=theme_score-? WHERE id=? AND theme_score IS NOT NULL',
                          (amount, taste_axes.identity(self.conn, facet)))
        self.conn.commit()

    @synchronized
    def get_theme(self, facet):
        row = self.conn.execute('SELECT theme_score FROM taste_axes WHERE id=?',
                                (taste_axes.identity(self.conn, facet),)).fetchone()
        return row[0] if row else None

    @synchronized
    def theme_rows(self) -> list:
        """All graduation-ledger rows {facet, score, updated_at}, strongest-magnitude first - the
        transient->permanent funnel, for the Taste-model transparency view."""
        return self.conn.execute(f'SELECT {taste_axes.token_expression()} facet,theme_score score,'
            'theme_updated_at updated_at FROM taste_axes a WHERE theme_score IS NOT NULL '
            'ORDER BY ABS(theme_score) DESC').fetchall()

    # --- graduation instrumentation: one row per threshold-crossing nudge (§1c model-health) ---
    @synchronized
    def log_graduation(self, axis, source, score, factor, new_weight, now) -> None:
        """Record one graduation event: the axis, the source signal that drove it, the ledger value at
        the crossing, the nudge factor, and the resulting permanent weight. Append-only; read by the
        model-health panel to show graduation counts by source and to tune source weights on evidence."""
        with self.conn.atomic(join=True):
            taste_axes.record_graduation(self.conn, axis, source, score, factor, new_weight, now)

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
        return self.conn.execute(f'SELECT id,axis,source,score,factor,new_weight,created_at FROM {taste_axes.GRADUATIONS}'
            f' {where} ORDER BY created_at DESC,id DESC LIMIT ?', [*args, limit]).fetchall()

    @synchronized
    def graduation_audit_rows(self) -> list:
        """Every graduation log row's (axis, source, factor), oldest-first, for source audits."""
        return self.conn.execute(f'SELECT axis,source,factor FROM {taste_axes.GRADUATIONS} ORDER BY id').fetchall()

    @synchronized
    def graduation_log_counts(self) -> dict:
        """{source: count} over all logged graduation events, for the model-health panel."""
        return {r["source"]: r["c"] for r in self.conn.execute(
            f'SELECT source,COUNT(*) c FROM {taste_axes.GRADUATIONS} GROUP BY source')}

    # --- play-exposure graduation: per-axis once-per-UTC-day stamp (the idempotency guard) ---
    @synchronized
    def get_play_graduated_day(self, axis):
        """The UTC day-string the play-exposure funnel last graduated `axis`, or None if never."""
        row = self.conn.execute('SELECT play_last_graduated_day FROM taste_axes WHERE id=?',
                                (taste_axes.identity(self.conn, axis),)).fetchone()
        return row[0] if row else None

    @synchronized
    def set_play_graduated_day(self, axis, day) -> None:
        """Stamp the UTC day the play-exposure funnel last touched `axis` (once-per-day idempotency)."""
        reference = taste_axes.identity(self.conn, axis, create=True)
        self.conn.execute('UPDATE taste_axes SET play_last_graduated_day=? WHERE id=?', (day, reference))
        self.conn.commit()
