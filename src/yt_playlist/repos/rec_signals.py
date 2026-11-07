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
        column = taste_axes.column(self.conn, 'facet')
        reference = taste_axes.reference(self.conn, 'rec_theme', facet, create=True)
        self.conn.execute(
            f"INSERT INTO rec_theme({column}, score, updated_at) VALUES (?,?,?) "
            f"ON CONFLICT({column}) DO UPDATE SET score = score + excluded.score, updated_at = excluded.updated_at",
            (reference, contribution, now))
        self.conn.commit()
        return self.get_theme(facet)

    @synchronized
    def discount_theme(self, facet, amount) -> None:
        self.conn.execute(f"UPDATE rec_theme SET score = score - ? WHERE {taste_axes.column(self.conn,'facet')}=?",
                          (amount, taste_axes.reference(self.conn,'rec_theme',facet)))
        self.conn.commit()

    @synchronized
    def get_theme(self, facet):
        row = taste_axes.lookup(self.conn,'rec_theme',facet,'sum(score) AS score')
        return row["score"] if row else None

    @synchronized
    def theme_rows(self) -> list:
        """All graduation-ledger rows {facet, score, updated_at}, strongest-magnitude first - the
        transient->permanent funnel, for the Taste-model transparency view."""
        return self.conn.execute(taste_axes.projection(self.conn,'rec_theme','facet,score,updated_at',token_name='facet') +
                                 ' ORDER BY ABS(score) DESC').fetchall()

    # --- graduation instrumentation: one row per threshold-crossing nudge (§1c model-health) ---
    @synchronized
    def log_graduation(self, axis, source, score, factor, new_weight, now) -> None:
        """Record one graduation event: the axis, the source signal that drove it, the ledger value at
        the crossing, the nudge factor, and the resulting permanent weight. Append-only; read by the
        model-health panel to show graduation counts by source and to tune source weights on evidence."""
        table, column = taste_axes.table(self.conn,'rec_grad_log'), taste_axes.column(self.conn)
        self.conn.execute(
            f"INSERT INTO {table}({column}, source, score, factor, new_weight, created_at) "
            "VALUES (?,?,?,?,?,?)", (taste_axes.identity(self.conn,axis,create=True), source, score, factor, new_weight, now))
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
        return self.conn.execute(taste_axes.projection(self.conn,taste_axes.table(self.conn,'rec_grad_log'),
            'id,axis,source,score,factor,new_weight,created_at') +
            f" {where} ORDER BY created_at DESC, t.id DESC LIMIT ?", [*args, limit]).fetchall()

    @synchronized
    def graduation_audit_rows(self) -> list:
        """Every graduation log row's (axis, source, factor), oldest-first, for source audits."""
        return self.conn.execute(taste_axes.projection(self.conn,taste_axes.table(self.conn,'rec_grad_log'),
            'axis,source,factor') + ' ORDER BY t.id').fetchall()

    @synchronized
    def graduation_log_counts(self) -> dict:
        """{source: count} over all logged graduation events, for the model-health panel."""
        return {r["source"]: r["c"] for r in self.conn.execute(
            'SELECT source,COUNT(*) c FROM ('+taste_axes.projection(self.conn,taste_axes.table(self.conn,'rec_grad_log'),'source')+') GROUP BY source').fetchall()}

    # --- play-exposure graduation: per-axis once-per-UTC-day stamp (the idempotency guard) ---
    @synchronized
    def get_play_graduated_day(self, axis):
        """The UTC day-string the play-exposure funnel last graduated `axis`, or None if never."""
        row = taste_axes.lookup(self.conn,'rec_play_grad',axis,'max(last_graduated_day) AS last_graduated_day')
        return row["last_graduated_day"] if row else None

    @synchronized
    def set_play_graduated_day(self, axis, day) -> None:
        """Stamp the UTC day the play-exposure funnel last touched `axis` (once-per-day idempotency)."""
        column, table = taste_axes.column(self.conn), taste_axes.table(self.conn,'rec_play_grad')
        reference = taste_axes.reference(self.conn,'rec_play_grad',axis,create=True)
        if taste_axes.compatible(self.conn):
            predicate,parameters = taste_axes.matches(self.conn,'rec_play_grad',axis)
            self.conn.execute('UPDATE '+table+' SET last_graduated_day=? WHERE '+predicate,(day,*parameters))
        self.conn.execute(
            f"INSERT INTO {table}({column}, last_graduated_day) VALUES (?,?) "
            f"ON CONFLICT({column}) DO UPDATE SET last_graduated_day=excluded.last_graduated_day",
            (reference, day))
        self.conn.commit()
