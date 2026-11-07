"""Rendered offers, observed responses, exposure counters and historical attribution.

Reports load a coherent set of detached inputs on a private snapshot. Recommendation
services own statistical interpretation; this repository owns persistence and retention.
"""
from yt_playlist.core import album_playback_context, playback_bindings, served_facts
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.repos.scorecard_attribution import ScorecardFacts
from yt_playlist.repos import policy_storage

RETENTION_BATCH_SIZE = 256


class RecLedgerRepo(Repo):
    @synchronized
    def mix_choices_available(self):
        from yt_playlist.core import mix_choices
        return mix_choices.available(self.conn)

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
    def album_context_available(self):
        return album_playback_context.available(self.conn)

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
    def playback_bindings_available(self):
        return playback_bindings.available(self.conn)

    @lookup_read
    def video_exists(self, video):
        return self.conn.execute('SELECT 1 FROM youtube_items WHERE video_id=?', (video,)).fetchone() is not None

    @synchronized
    def put_choice_binding(self, account_id, playlist, video, opportunity, option, now, *, before, limit):
        with self.conn.atomic():
            playback_bindings.insert(self.conn, account_id, playlist, video, opportunity, option, now,
                                     before=before, limit=limit)

    @lookup_read
    def choice_binding(self, account_id, playlist, video, *, before, through):
        return playback_bindings.lookup(self.conn, account_id, playlist, video, before=before, through=through)

    @synchronized
    def library_recording_count(self):
        return self.conn.execute(
            'SELECT COUNT(DISTINCT e.recording_id) FROM library_memberships m '
            'JOIN catalog_entries e ON e.id=m.entry_id').fetchone()[0]

    @synchronized
    def scorecard_impressions(self, start, end, *, impression_id, experiments_only):
        """Detach impressions in the supplied report range."""
        live = served_facts.expression(self.conn, 'policy_kind', "COALESCE(json_extract(provenance,'$.policy_kind'),'live')")
        comparison = served_facts.expression(self.conn, 'comparison_id', "json_extract(provenance,'$.experiment.comparison_id')")
        columns = '*'
        if served_facts.available(self.conn):
            columns = ('id,request_id,surface,lane,mode_id,identity_key,rank,score,model_version,served_at,'
                       'recording_id,raw_identity_key,CASE WHEN EXISTS('
                       'SELECT 1 FROM served_facts f WHERE f.impression_id=id) THEN \'{}\' '
                       'ELSE provenance END provenance')
        impressions = [dict(r) for r in self.conn.execute(
            f"SELECT {columns} FROM rec_served_impressions WHERE served_at>=? AND served_at<? "
            f"AND {live}='live' AND (?=0 OR {comparison} IS NOT NULL) "
            "AND (? IS NULL OR id=?) ORDER BY served_at,id",
            (start, end, experiments_only, impression_id, impression_id)).fetchall()]
        if served_facts.available(self.conn):
            facts = {row['impression_id']: row for row in self.conn.execute(
                'SELECT f.* FROM served_facts f JOIN rec_served_impressions i ON i.id=f.impression_id '
                'WHERE i.served_at>=? AND i.served_at<?', (start, end))}
            for row in impressions:
                if row['id'] in facts:
                    row['facts'] = dict(facts[row['id']])
        return impressions

    def scorecard_facts(self):
        return ScorecardFacts(self._store)

    @synchronized
    def served_recommendation_facts(self, impression_id):
        row = self.conn.execute(
            "SELECT i.* FROM rec_served_impressions i WHERE i.id=?",
            (impression_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['facts'] = None
        if served_facts.available(self.conn):
            fact = self.conn.execute('SELECT * FROM served_facts WHERE impression_id=?', (impression_id,)).fetchone()
            if fact is not None:
                result['facts'] = dict(fact)
        return result

    @synchronized
    def experiment_shadows(self, start, end):
        ranker = served_facts.expression(self.conn, 'ranker', "json_extract(provenance,'$.ranker')")
        comparison = served_facts.expression(self.conn, 'comparison_id', "json_extract(provenance,'$.experiment.comparison_id')")
        kind = served_facts.expression(self.conn, 'policy_kind', "json_extract(provenance,'$.policy_kind')")
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
    def insert_served_impressions(self, rows, *, versions):
        with self.conn.atomic():
            if rows:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO rec_served_impressions("
                    "request_id,surface,lane,mode_id,identity_key,rank,score,model_version,provenance,served_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                policy_storage.refresh_references(self.conn, versions)

    @lookup_read
    def served_facts_active(self):
        return served_facts.active(self.conn)

    @lookup_read
    def served_facts_available(self):
        return served_facts.available(self.conn)

    @synchronized
    def reserve_impression_ids(self, rows):
        with self.conn.atomic():
            identity = self.conn.execute('SELECT next_id FROM served_fact_state WHERE id=1').fetchone()[0]
            identity = max(identity, self.conn.execute('SELECT COALESCE(max(id),0)+1 FROM rec_served_impressions').fetchone()[0])
            reserved, seen = [], set()
            for index, row in enumerate(rows):
                key = row[0], row[1], row[5]
                if key in seen or self.conn.execute(
                        'SELECT 1 FROM rec_served_impressions WHERE request_id=? AND surface=? AND rank=?', key).fetchone():
                    continue
                seen.add(key)
                reserved.append((identity, index))
                identity += 1
            self.conn.execute('UPDATE served_fact_state SET next_id=? WHERE id=1', (identity,))
            return reserved

    @synchronized
    def insert_reserved_impressions(self, rows, *, versions):
        with self.conn.atomic():
            for identity, row, facts in rows:
                inserted = self.conn.execute(
                    'INSERT INTO rec_served_impressions(id,request_id,surface,lane,mode_id,identity_key,rank,'
                    'score,model_version,provenance,served_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(request_id,surface,rank) DO NOTHING', (identity, *row))
                if inserted.rowcount:
                    self.conn.execute('INSERT INTO served_facts(impression_id,' + ','.join(served_facts.FIELDS) +
                        ') VALUES (' + ','.join('?' for _ in range(1 + len(served_facts.FIELDS))) + ')', (identity, *facts))
                    self.conn.execute('DELETE FROM served_diagnostic_retirements WHERE impression_id=?', (identity,))
            policy_storage.refresh_references(self.conn, versions)

    @synchronized
    def _prune_impression_batch(self, before):
        expired = self.conn.execute('SELECT id,model_version,json_extract(provenance,\'$.pool_policy\') '
            'FROM rec_served_impressions WHERE served_at<? ORDER BY served_at,id LIMIT ?',
            (before, RETENTION_BATCH_SIZE)).fetchall()
        with self.conn:
            self.conn.executemany('DELETE FROM rec_served_impressions WHERE id=?', [(row[0],) for row in expired])
            policy_storage.refresh_references(self.conn,
                [version for row in expired for version in
                 (row[1], str(row[2]) if row[2] is not None else None)])

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
        kind = served_facts.expression(self.conn, 'policy_kind', "COALESCE(json_extract(provenance,'$.policy_kind'),'live')")
        rows = self.conn.execute(
            "SELECT lane,COUNT(*) c FROM rec_served_impressions WHERE (? IS NULL OR served_at>=?) "
            f"AND {kind}='live' GROUP BY lane",
            (since, since)).fetchall()
        return {r["lane"]: r["c"] for r in rows}

    @synchronized
    def expire_diagnostics(self, before, *, limit):
        with self.conn.atomic():
            identities = [row[0] for row in self.conn.execute('SELECT f.impression_id FROM rec_served_impressions i '
                'JOIN served_facts f ON f.impression_id=i.id WHERE i.served_at<? AND f.diagnostic_expired=0 '
                'ORDER BY i.served_at,i.id LIMIT ?', (before, limit))]
            self.conn.executemany('UPDATE served_facts SET diagnostic_expired=1 WHERE impression_id=?', [(i,) for i in identities])
            self.conn.executemany('INSERT OR IGNORE INTO served_diagnostic_retirements VALUES (?)', [(i,) for i in identities])

    @synchronized
    def diagnostic_retirements(self, *, limit):
        return [row[0] for row in self.conn.execute('SELECT r.impression_id FROM served_diagnostic_retirements r '
            'LEFT JOIN served_facts f ON f.impression_id=r.impression_id '
            'WHERE f.impression_id IS NULL OR f.diagnostic_expired=1 ORDER BY r.impression_id LIMIT ?', (limit,))]

    @synchronized
    def acknowledge_diagnostic_retirements(self, identities):
        with self.conn.atomic():
            self.conn.executemany('DELETE FROM served_diagnostic_retirements WHERE impression_id=?', [(i,) for i in identities])

    @synchronized
    def existing_impressions(self, identities):
        if not identities:
            return set()
        return {row[0] for row in self.conn.execute('SELECT id FROM rec_served_impressions WHERE id IN (' +
            ','.join('?' for _ in identities) + ')', identities)}

    @synchronized
    def story_views(self) -> dict:
        """Actual visible story appearances, separate from Home visits and prefetches."""
        return {row['item_key']: {'views': row['views'], 'last_shown': row['last_shown']}
                for row in self.conn.execute(
                    "SELECT item_key,views,last_shown FROM rec_impressions WHERE surface='into_recently'")}

    @synchronized
    def record_story_views(self, subjects, now, *, surface, cooldown_s) -> bool:
        """Increment views only after the caller's supplied cooldown."""
        changed = False
        for subject in subjects:
            cursor = self.conn.execute(
                "INSERT INTO rec_impressions(surface,item_key,views,last_shown) "
                "VALUES (?,?,1,?) ON CONFLICT(surface,item_key) DO UPDATE "
                "SET views=rec_impressions.views+1,last_shown=excluded.last_shown "
                "WHERE rec_impressions.last_shown<=excluded.last_shown-?", (surface, subject, now, cooldown_s))
            changed |= cursor.rowcount > 0
        self.conn.commit()
        return changed

    # --- per-card rotation (the rec_impressions table, surface='card') ---
    @synchronized
    def bump_card_view(self, card, now) -> int:
        """Count one real view of a Home card (one row per card, surface='card') and return its new
        total. Drives per-card rotation: a card holds its content for erosion_view_cap views, then
        epoch = (views-1)//cap advances and it regenerates. Ticked once per genuine Home visit
        (never on steer/stance previews) so tuning your taste model doesn't churn the cards."""
        with self.conn.atomic():
            return self.conn.execute("INSERT INTO rec_impressions(surface,item_key,views,last_shown) "
                "VALUES('card',?,1,?) ON CONFLICT(surface,item_key) DO UPDATE SET "
                "views=rec_impressions.views+1,last_shown=excluded.last_shown RETURNING views", (card, now)).fetchone()[0]

    @lookup_read
    def card_views(self, card) -> int:
        """Current view total for a Home card (0 if never shown), read-only, so previews and
        re-renders can compute the card's rotation epoch without advancing it."""
        row = self.conn.execute(
            "SELECT views FROM rec_impressions WHERE surface='card' AND item_key=?", (card,)).fetchone()
        return row["views"] if row else 0

    @synchronized
    def set_card_views(self, card, views, now) -> None:
        """Persist the view count calculated inside the caller's write unit."""
        self.conn.execute("INSERT INTO rec_impressions(surface,item_key,views,last_shown) "
                          "VALUES('card',?,?,?) ON CONFLICT(surface,item_key) DO UPDATE "
                          "SET views=excluded.views,last_shown=excluded.last_shown", (card, views, now))
        self.conn.commit()
