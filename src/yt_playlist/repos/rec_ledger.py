"""Rendered offers, observed responses, exposure counters and historical attribution.

Reports load a coherent set of detached inputs on a private snapshot. Recommendation
services own statistical interpretation; this repository owns persistence and retention.
"""
import json

from yt_playlist.repos.base import Repo, synchronized, snapshot_read
from yt_playlist.repos.scorecard_attribution import Attribution

RESPONSE_RETAIN_DAYS = 90
RETENTION_BATCH_SIZE = 256
POLICY_RETENTION_CURSOR = 'rec_policy_retention_cursor'


class RecLedgerRepo(Repo):
    @synchronized
    def bind_choice_playback(self, identity_id, playlist, video, opportunity, option, now, window_s):
        """Small exact-context index for delayed player observations; raw evidence stays in JSON.

        Retain recent track bindings across a restart and a direction change. Evicted or expired
        observations remain unattributed. The bound is a storage/work limit, not a taste weight.
        """
        rows = json.loads(self._store.settings.get_setting('listening_playback_bindings') or '[]')
        key = (identity_id, playlist, video)
        rows = [row for row in rows if 0 <= now - row['at'] <= window_s
                and (row['identity_id'], row['playlist'], row['video']) != key]
        rows.append({'identity_id': identity_id, 'playlist': playlist, 'video': video,
                     'opportunity_id': opportunity, 'option_id': str(option), 'at': now})
        self._store.settings.set_setting('listening_playback_bindings', json.dumps(rows[-64:]))

    @synchronized
    def choice_playback(self, identity_id, playlist, video, now, window_s):
        rows = json.loads(self._store.settings.get_setting('listening_playback_bindings') or '[]')
        return next((row for row in reversed(rows)
                     if (row['identity_id'], row['playlist']) == (identity_id, playlist)
                     and (video is None or row['video'] == video)
                     and 0 <= now - row['at'] <= window_s), None)

    @synchronized
    def evidence(self):
        """Size replay evidence from current library membership, including after imports."""
        from yt_playlist.core.evidence_ledger import EvidenceLedger, library_byte_budget
        from yt_playlist.core import paths
        store = self._store
        directory = (store._db_path.parent / (store._db_path.name + ".evidence") if store._db_path
                     else paths.data_dir() / "test-evidence" / store._evidence_key)
        recordings = self.conn.execute(
            'SELECT COUNT(DISTINCT e.recording_id) FROM library_memberships m '
            'JOIN catalog_entries e ON e.id=m.entry_id').fetchone()[0]
        return EvidenceLedger(directory, library_byte_budget(recordings))

    @synchronized
    def publish_prepared_policies(self, rows) -> None:
        """Join the snapshot publisher's transaction without committing proposals early."""
        self.conn.executemany("INSERT OR IGNORE INTO rec_serving_policies VALUES (?,?,?)", rows)

    @snapshot_read
    def scorecard_inputs(self, start, end, outcome_window_h=24,
                         impression_id=None, experiments_only=False):
        """Detach one coherent live impression range and its historical outcome ownership."""
        impressions = [dict(r) for r in self.conn.execute(
            "SELECT * FROM rec_served_impressions WHERE served_at>=? AND served_at<? "
            "AND COALESCE(json_extract(provenance,'$.policy_kind'),'live')='live' "
            "AND (?=0 OR json_extract(provenance,'$.experiment.comparison_id') IS NOT NULL) "
            "AND (? IS NULL OR id=?) ORDER BY served_at,id",
            (start, end, experiments_only, impression_id, impression_id)).fetchall()]
        horizon = max(0.0, float(outcome_window_h)) * 3600.0
        attribution = Attribution(self.conn, {r['identity_key'] for r in impressions}, start, end, horizon)
        return impressions, attribution

    @snapshot_read
    def served_recommendation_inputs(self, impression_id):
        row = self.conn.execute(
            "SELECT i.*,p.configuration FROM rec_served_impressions i "
            "LEFT JOIN rec_serving_policies p USING(model_version) WHERE i.id=?",
            (impression_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["provenance"] = json.loads(result["provenance"])
        result["configuration"] = json.loads(result["configuration"]) if result["configuration"] else None
        pool = result["provenance"].get("pool_policy")
        pool_row = self.conn.execute("SELECT configuration FROM rec_serving_policies WHERE model_version=?",
                                     (pool,)).fetchone() if pool else None
        result["pool_configuration"] = json.loads(pool_row[0]) if pool_row else None
        inputs = (None if result["provenance"].get("policy_kind") == "counterfactual" else
                  self.scorecard_inputs(result["served_at"], result["served_at"] + 1,
                                        impression_id=impression_id))
        return result, inputs

    @snapshot_read
    def experiment_inputs(self, start, end, mature_end):
        inputs = self.scorecard_inputs(start, mature_end, experiments_only=True)
        shadows = self.conn.execute(
            "SELECT json_extract(provenance,'$.ranker') ranker,COUNT(*) rows,"
            "COUNT(DISTINCT json_extract(provenance,'$.experiment.comparison_id')) opportunities "
            "FROM rec_served_impressions WHERE served_at>=? AND served_at<? "
            "AND json_extract(provenance,'$.policy_kind')='counterfactual' GROUP BY ranker",
            (start, end)).fetchall()
        return inputs, [dict(r) for r in shadows]

    @synchronized
    def claim_home_offers(self, epoch, signatures, now):
        """Deduplicate actual offers; home_shown is the ledger's reserved proposal key."""
        row = self.conn.execute("SELECT payload FROM rec_proposals WHERE surface='home_shown'").fetchone()
        try:
            previous = json.loads(row[0]) if row else {}
        except (ValueError, TypeError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        shown = set(previous.get("signatures", [])) if previous.get("epoch") == epoch else set()
        unseen = set(signatures) - shown
        if unseen:
            # Only the current window is retained; a new window is a new actual opportunity.
            self.conn.execute(
                "INSERT INTO rec_proposals(surface,payload,built_at) VALUES ('home_shown',?,?) "
                "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at",
                (json.dumps({"epoch": epoch, "signatures": sorted(shown | unseen)[-100:]}), now))
            self.conn.commit()
        return unseen

    # --- #113 served recommendation -> observed outcome ledger -------------------------------
    @synchronized
    def record_serving_policy(self, version, configuration, now) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO rec_serving_policies VALUES (?,?,?)",
            (version, json.dumps(configuration, sort_keys=True), now))
        self.conn.commit()

    @synchronized
    def record_prepared_policies(self, rows) -> None:
        """Store policy JSON that was calculated and encoded off the serving connection."""
        self.conn.executemany("INSERT OR IGNORE INTO rec_serving_policies VALUES (?,?,?)", rows)
        self.conn.commit()

    def record_recommendation_outcomes(self, event_id, kind, keys, now, generated=False) -> None:
        if kind not in {"open", "start", "save", "promotion"}:
            raise ValueError("unsupported recommendation response")
        rows = [(event_id, kind, key, now, int(generated)) for key in set(keys) if key]
        self._insert_recommendation_outcomes(rows)
        self._prune_response_batch(now - RESPONSE_RETAIN_DAYS * 86400)

    @synchronized
    def _insert_recommendation_outcomes(self, rows):
        self.conn.executemany(
            "INSERT OR IGNORE INTO rec_response_events(event_id,kind,identity_key,at,is_generated) "
            "VALUES (?,?,?,?,?)", rows)
        self.conn.commit()

    def record_served_impressions(self, request_id, surface, items, now, prune_before=None) -> None:
        """Persist final live order or an explicitly marked counterfactual replay.

        ``items`` are dictionaries containing lane/key/rank plus optional mode_id, score and
        provenance. The request/rank uniqueness key makes an HTMX retry idempotent. Instrumentation
        is fail-open at route call sites. Retention catches up in bounded batches,
        releasing the serving lock between insertion and each cleanup transaction.
        """
        rows = []
        for item in items:
            key = item.get("identity_key") or item.get("key")
            if not key:
                continue
            rows.append((request_id, surface, item.get("lane") or "", item.get("mode_id"), key,
                         int(item["rank"]), item.get("score"), item.get("model_version") or "unknown",
                         json.dumps(item.get("provenance") or {}, sort_keys=True), now))
        self._insert_served_impressions(rows)
        if prune_before is not None:
            self._prune_impression_batch(prune_before)
            self._prune_response_batch(prune_before)
            self._prune_policy_batch(prune_before)

    @synchronized
    def _insert_served_impressions(self, rows):
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO rec_served_impressions("
                "request_id,surface,lane,mode_id,identity_key,rank,score,model_version,provenance,served_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    @synchronized
    def _prune_impression_batch(self, before):
        self.conn.execute(
            "DELETE FROM rec_served_impressions WHERE id IN ("
            "SELECT id FROM rec_served_impressions WHERE served_at<? ORDER BY served_at,id LIMIT ?)",
            (before, RETENTION_BATCH_SIZE))
        self.conn.commit()

    @synchronized
    def _prune_response_batch(self, before):
        self.conn.execute(
            "DELETE FROM rec_response_events WHERE id IN ("
            "SELECT id FROM rec_response_events WHERE at<? ORDER BY at,id LIMIT ?)",
            (before, RETENTION_BATCH_SIZE))
        self.conn.commit()

    @synchronized
    def _prune_policy_batch(self, before):
        row = self.conn.execute('SELECT value FROM settings WHERE key=?', (POLICY_RETENTION_CURSOR,)).fetchone()
        try:
            cursor = json.loads(row[0]) if row else None
        except (ValueError, TypeError):
            cursor = None
        if not (isinstance(cursor, list) and len(cursor) == 2
                and isinstance(cursor[0], (float, int)) and isinstance(cursor[1], str)):
            cursor = None
        after = ' AND (created_at,model_version)>(?,?)' if cursor else ''
        candidates = self.conn.execute(
            'SELECT created_at,model_version FROM rec_serving_policies WHERE created_at<?' + after +
            ' ORDER BY created_at,model_version LIMIT ?',
            (before, *cursor, RETENTION_BATCH_SIZE) if cursor else (before, RETENTION_BATCH_SIZE)).fetchall()
        if candidates:
            # Indexed existence checks retain both immutable policy references.
            # Do not scan JSON across the ledger or delete a policy still needed
            # by an expired impression that hasn't reached its cleanup batch yet.
            placeholders = ','.join('?' for _ in candidates)
            self.conn.execute(
                f'DELETE FROM rec_serving_policies WHERE model_version IN ({placeholders}) '
                'AND NOT EXISTS (SELECT 1 FROM rec_served_impressions i '
                'WHERE i.model_version=rec_serving_policies.model_version) '
                'AND NOT EXISTS (SELECT 1 FROM rec_served_impressions i '
                "WHERE CAST(json_extract(i.provenance,'$.pool_policy') AS TEXT)=rec_serving_policies.model_version)",
                [r['model_version'] for r in candidates])
        if len(candidates) == RETENTION_BATCH_SIZE:
            # Persist the scan position so referenced old policies cannot starve
            # later orphans, including when the app restarts during catch-up.
            last = candidates[-1]
            self.conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                              'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                              (POLICY_RETENTION_CURSOR, json.dumps([last['created_at'], last['model_version']])))
        elif row:
            self.conn.execute('DELETE FROM settings WHERE key=?', (POLICY_RETENTION_CURSOR,))
        self.conn.commit()

    @synchronized
    def lane_impression_counts(self, since=None) -> dict:
        """Actual exposure counts from the unified ledger, excluding replay rows."""
        rows = self.conn.execute(
            "SELECT lane,COUNT(*) c FROM rec_served_impressions WHERE (? IS NULL OR served_at>=?) "
            "AND COALESCE(json_extract(provenance,'$.policy_kind'),'live')='live' GROUP BY lane",
            (since, since)).fetchall()
        return {r["lane"]: r["c"] for r in rows}

    @synchronized
    def story_views(self) -> dict:
        """Actual visible story appearances, separate from Home visits and prefetches."""
        return {row['item_key']: {'views': row['views'], 'last_shown': row['last_shown']}
                for row in self.conn.execute(
                    "SELECT item_key,views,last_shown FROM rec_impressions WHERE surface='into_recently'")}

    @synchronized
    def record_story_views(self, subjects, now) -> bool:
        """Collapse fragment replacements, retries and repeated visits within six hours."""
        changed = False
        for subject in set(subjects):
            cursor = self.conn.execute(
                "INSERT INTO rec_impressions(surface,item_key,views,last_shown) "
                "VALUES ('into_recently',?,1,?) ON CONFLICT(surface,item_key) DO UPDATE "
                "SET views=rec_impressions.views+1,last_shown=excluded.last_shown "
                "WHERE rec_impressions.last_shown<=excluded.last_shown-21600", (subject, now))
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
        row = self.conn.execute(
            "SELECT views FROM rec_impressions WHERE surface='card' AND item_key=?", (card,)).fetchone()
        n = (row["views"] + 1) if row else 1
        if row:
            self.conn.execute("UPDATE rec_impressions SET views=?, last_shown=? "
                              "WHERE surface='card' AND item_key=?", (n, now, card))
        else:
            self.conn.execute("INSERT INTO rec_impressions(surface,item_key,views,last_shown) "
                              "VALUES('card',?,1,?)", (card, now))
        self.conn.commit()
        return n

    @synchronized
    def card_views(self, card) -> int:
        """Current view total for a Home card (0 if never shown), read-only, so previews and
        re-renders can compute the card's rotation epoch without advancing it."""
        row = self.conn.execute(
            "SELECT views FROM rec_impressions WHERE surface='card' AND item_key=?", (card,)).fetchone()
        return row["views"] if row else 0

    @synchronized
    def refresh_card(self, card, cap, now) -> None:
        """Refresh button: jump a Home card to the START of its NEXT rotation epoch (a fresh, unseen
        slice) and reset its view clock there, so it holds the new slice for the next `cap` views.
        epoch = (views-1)//cap, so landing at (epoch+1)*cap+1 advances one epoch and resets the clock."""
        cap = max(1, cap)
        row = self.conn.execute(
            "SELECT views FROM rec_impressions WHERE surface='card' AND item_key=?", (card,)).fetchone()
        cur = row["views"] if row else 0
        # current epoch is (cur-1)//cap; jump to the first view of the next epoch: (epoch+1)*cap + 1
        new_views = (max(0, cur - 1) // cap + 1) * cap + 1
        if row:
            self.conn.execute("UPDATE rec_impressions SET views=?, last_shown=? "
                              "WHERE surface='card' AND item_key=?", (new_views, now, card))
        else:
            self.conn.execute("INSERT INTO rec_impressions(surface,item_key,views,last_shown) "
                              "VALUES('card',?,?,?)", (card, new_views, now))
        self.conn.commit()
