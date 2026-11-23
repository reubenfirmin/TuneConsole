"""Rendered offers, observed responses, exposure counters and historical attribution.

Reports load a coherent set of detached inputs on a private snapshot. Recommendation
services own statistical interpretation; this repository owns persistence and retention.
"""
from yt_playlist.core import album_playback_context, playback_bindings, served_facts
from yt_playlist.core import recommendation_impressions, recording_library
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.repos.scorecard_attribution import ScorecardFacts

RETENTION_BATCH_SIZE = 256
IMPRESSION_FIELDS = ('id', 'request_id', 'surface', 'lane', 'mode_id', 'identity_key', 'rank', 'score',
                     'model_version', 'provenance', 'served_at')


def _impression(row):
    return {field: row[field] for field in (*IMPRESSION_FIELDS, 'recording_id', 'raw_identity_key')} | {
        'facts': served_facts.captured(row)}


class RecLedgerRepo(Repo):
    @synchronized
    def publish_mix_menu(self, plan):
        from yt_playlist.core import mix_choices
        with self.conn.atomic():
            mix_choices.insert_menu(self.conn, plan)

    @synchronized
    def mix_projection_facts(self, opportunity_id, context_id, *, mixes):
        from yt_playlist.core import mix_choices
        return mix_choices.projection_facts(self.conn, opportunity_id, context_id, mixes=mixes)

    @snapshot_read
    def mix_descriptor(self, descriptor_id):
        from yt_playlist.core import mix_choices
        return mix_choices.read_descriptor(self.conn, descriptor_id)

    @snapshot_read
    def album_context_values(self):
        return album_playback_context.load_native(self.conn)

    @synchronized
    def put_album_context(self, value, album_id):
        with self.conn.atomic():
            album_playback_context.put_native(self.conn, value, album_id)

    @synchronized
    def bind_album_context(self, pending, account_id):
        with self.conn.atomic():
            album_playback_context.bind_native(self.conn, pending, account_id)

    @lookup_read
    def has_served_request(self, request_id):
        return self.conn.execute('SELECT 1 FROM rec_served_impressions WHERE request_id=? LIMIT 1',
                                 (request_id,)).fetchone() is not None

    @lookup_read
    def video_exists(self, video):
        return self.conn.execute('SELECT 1 FROM track_sources WHERE video_id=?', (video,)).fetchone() is not None

    @synchronized
    def put_choice_binding(self, account_id, playlist, video, opportunity, option, now, *, before, limit,
                           response_id=None):
        with self.conn.atomic():
            playback_bindings.insert(self.conn, account_id, playlist, video, opportunity, option, now,
                                     before=before, limit=limit, response_id=response_id)

    @lookup_read
    def choice_binding(self, account_id, playlist, video, *, before, through):
        return playback_bindings.lookup(self.conn, account_id, playlist, video, before=before, through=through)

    @synchronized
    def library_recording_count(self):
        library = recording_library.RECORDINGS_SQL
        return self.conn.execute(f'SELECT COUNT(*) FROM ({library})').fetchone()[0]

    @synchronized
    def scorecard_impressions(self, start, end, *, impression_id, experiments_only):
        """Detach impressions in the supplied report range."""
        live = served_facts.expression('policy_kind', "COALESCE(json_extract(provenance,'$.policy_kind'),'live')")
        comparison = served_facts.expression('comparison_id', "json_extract(provenance,'$.experiment.comparison_id')")
        rows = self.conn.execute(
            "SELECT * FROM rec_served_impressions WHERE served_at>=? AND served_at<? "
            f"AND {live}='live' AND (?=0 OR {comparison} IS NOT NULL) "
            "AND (? IS NULL OR id=?) ORDER BY served_at,id",
            (start, end, experiments_only, impression_id, impression_id))
        return [_impression(row) for row in rows]

    def scorecard_facts(self):
        return ScorecardFacts(self._store)

    @synchronized
    def served_recommendation_facts(self, impression_id):
        row = self.conn.execute('SELECT * FROM rec_served_impressions WHERE id=?', (impression_id,)).fetchone()
        return _impression(row) if row is not None else None

    @synchronized
    def experiment_shadows(self, start, end):
        ranker = served_facts.expression('ranker', "json_extract(provenance,'$.ranker')")
        comparison = served_facts.expression('comparison_id', "json_extract(provenance,'$.experiment.comparison_id')")
        kind = served_facts.expression('policy_kind', "json_extract(provenance,'$.policy_kind')")
        shadows = self.conn.execute(
            f"SELECT {ranker} ranker,COUNT(*) rows,COUNT(DISTINCT {comparison}) opportunities "
            "FROM rec_served_impressions WHERE served_at>=? AND served_at<? "
            f"AND {kind}='counterfactual' GROUP BY ranker",
            (start, end)).fetchall()
        return [dict(r) for r in shadows]

    @synchronized
    def insert_recommendation_outcomes(self, rows):
        self.conn.executemany(
            "INSERT OR IGNORE INTO rec_response_events(event_id,kind,identity_key,at,is_generated) "
            "VALUES (?,?,?,?,?)", rows)
        self.conn.commit()

    @synchronized
    def reserve_impression_ids(self, rows):
        with self.conn.atomic():
            identity = self.conn.execute(
                "SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='rec_served_impressions'),0)+1"
            ).fetchone()[0]
            reserved, seen = [], set()
            for index, row in enumerate(rows):
                key = row[0], row[1], row[5]
                if key in seen or self.conn.execute(
                        'SELECT 1 FROM rec_served_impressions WHERE request_id=? AND surface=? AND rank=?', key).fetchone():
                    continue
                seen.add(key)
                reserved.append((identity, index))
                identity += 1
            if reserved:
                updated = self.conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='rec_served_impressions'",
                                            (identity - 1,))
                if not updated.rowcount:
                    self.conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('rec_served_impressions',?)",
                                      (identity - 1,))
            return reserved

    @synchronized
    def insert_reserved_impressions(self, rows, *, versions):
        columns = (*IMPRESSION_FIELDS, *served_facts.FIELDS)
        with self.conn.atomic():
            self.conn.executemany(
                'INSERT INTO rec_served_impressions(' + ','.join(columns) + ') VALUES ('
                + ','.join('?' for _ in columns) + ') ON CONFLICT(request_id,surface,rank) DO NOTHING',
                ((identity, *row, *facts) for identity, row, facts in rows))

    @synchronized
    def _prune_impression_batch(self, before):
        expired = self.conn.execute('SELECT id '
            'FROM rec_served_impressions WHERE served_at<? ORDER BY served_at,id LIMIT ?',
            (before, RETENTION_BATCH_SIZE)).fetchall()
        with self.conn:
            self.conn.executemany('DELETE FROM rec_served_impressions WHERE id=?', [(row[0],) for row in expired])

    @synchronized
    def _prune_response_batch(self, before):
        self.conn.execute(
            "DELETE FROM rec_response_events WHERE id IN ("
            "SELECT id FROM rec_response_events WHERE at<? ORDER BY at,id LIMIT ?)",
            (before, RETENTION_BATCH_SIZE))
        self.conn.commit()

    @synchronized
    def lane_impression_counts(self, since=None) -> dict:
        """Actual exposure counts from the unified ledger, excluding replay rows."""
        kind = served_facts.expression('policy_kind', "COALESCE(json_extract(provenance,'$.policy_kind'),'live')")
        rows = self.conn.execute(
            "SELECT lane,COUNT(*) c FROM rec_served_impressions WHERE (? IS NULL OR served_at>=?) "
            f"AND {kind}='live' GROUP BY lane",
            (since, since)).fetchall()
        return {r["lane"]: r["c"] for r in rows}

    @synchronized
    def expire_diagnostics(self, before, *, limit):
        with self.conn.atomic():
            return [row[0] for row in self.conn.execute('UPDATE rec_served_impressions SET diagnostic_expired=1 '
                'WHERE id IN (SELECT id FROM rec_served_impressions WHERE served_at<? '
                'AND policy_kind IS NOT NULL AND diagnostic_expired=0 ORDER BY served_at,id LIMIT ?) RETURNING id',
                (before, limit))]

    @synchronized
    def active_diagnostics(self, identities):
        if not identities:
            return set()
        return {row[0] for row in self.conn.execute('SELECT id FROM rec_served_impressions WHERE '
            'policy_kind IS NOT NULL AND diagnostic_expired=0 AND id IN ('
            + ','.join('?' for _ in identities) + ')', identities)}

    @synchronized
    def story_views(self) -> dict:
        """Actual visible story appearances, separate from Home visits and prefetches."""
        values = recommendation_impressions.counts(self.conn, recommendation_impressions.STORY)
        return {subject: {'views': views, 'last_shown': at} for subject, (views, at) in values.items()}

    @synchronized
    def record_story_views(self, subjects, now, *, surface, cooldown_s) -> bool:
        """Increment views only after the caller's supplied cooldown."""
        with self.conn.atomic(join=True):
            return recommendation_impressions.advance(self.conn, surface, subjects, now,
                                                      eligible=lambda at: at <= now - cooldown_s)

    @synchronized
    def bump_card_view(self, card, now) -> int:
        """Count one real view of a Home card (one row per card, surface='card') and return its new
        total. Drives per-card rotation: a card holds its content for erosion_view_cap views, then
        epoch = (views-1)//cap advances and it regenerates. Ticked once per genuine Home visit
        (never on steer/stance previews) so tuning your taste model doesn't churn the cards."""
        with self.conn.atomic():
            values = recommendation_impressions.counts(self.conn, recommendation_impressions.CARD)
            views = values.get(card, (0, None))[0] + 1
            values[card] = [views, now]
            recommendation_impressions.save(self.conn, recommendation_impressions.CARD, values)
            return views

    @lookup_read
    def card_views(self, card) -> int:
        """Current view total for a Home card (0 if never shown), read-only, so previews and
        re-renders can compute the card's rotation epoch without advancing it."""
        values = recommendation_impressions.counts(self.conn, recommendation_impressions.CARD)
        return values.get(card, (0, None))[0]

    @synchronized
    def bump_card_views(self, cards, now, *, overrides):
        """Advance all counters belonging to one Home visit together."""
        with self.conn.atomic():
            values = recommendation_impressions.counts(self.conn, recommendation_impressions.CARD)
            counts = {}
            for card in cards:
                views, at = values.get(card, (0, None))
                override = overrides.get(card)
                counts[card] = views + 1 if override is None else override
                values[card] = [counts[card], now if at is None else max(at, now)]
            recommendation_impressions.save(self.conn, recommendation_impressions.CARD, values)
            return counts

    @synchronized
    def set_card_views(self, card, views, now) -> None:
        """Persist the view count calculated inside the caller's write unit."""
        with self.conn.atomic(join=True):
            values = recommendation_impressions.counts(self.conn, recommendation_impressions.CARD)
            values[card] = [views, now]
            recommendation_impressions.save(self.conn, recommendation_impressions.CARD, values)
