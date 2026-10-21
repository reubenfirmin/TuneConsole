"""RecSurfaceRepo: the recommendation *serving* surfaces: impression counts (anti-staleness
erosion), materialized last-good proposals, and the Last.fm similar-artist cache.

Tables are created by numbered migrations before repositories are bound.
"""
import json
import math
import statistics

from yt_playlist.library.listen_derive import classify_exit

from yt_playlist.repos.base import Repo, synchronized

MOOD_EVENT_CAP = 200   # bound the rec_mood table (count, not age); this repo owns that table
RESPONSE_RETAIN_DAYS = 90
MIN_EXPERIMENT_CLUSTERS = 100


def _wilson(hits, n):
    if not n:
        return None
    z = 1.96
    p = hits / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [max(0, center - radius), min(1, center + radius)]


class RecSurfaceRepo(Repo):
    # --- #113 served recommendation -> observed outcome ledger -------------------------------
    @synchronized
    def record_serving_policy(self, version, configuration, now) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO rec_serving_policies VALUES (?,?,?)",
            (version, json.dumps(configuration, sort_keys=True), now))
        self.conn.commit()

    @synchronized
    def served_recommendation(self, impression_id):
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
        result["outcomes"] = (None if result["provenance"].get("policy_kind") == "counterfactual" else
            self.recommendation_scorecard(result["served_at"], result["served_at"] + 1,
                                         impression_id=impression_id))
        return result

    @synchronized
    def record_recommendation_outcomes(self, event_id, kind, keys, now, generated=False) -> None:
        if kind not in {"open", "start", "save", "promotion"}:
            raise ValueError("unsupported recommendation response")
        self.conn.executemany(
            "INSERT OR IGNORE INTO rec_response_events(event_id,kind,identity_key,at,is_generated) "
            "VALUES (?,?,?,?,?)", [(event_id, kind, key, now, int(generated)) for key in set(keys) if key])
        self.conn.execute("DELETE FROM rec_response_events WHERE at < ?",
                          (now - RESPONSE_RETAIN_DAYS * 86400,))
        self.conn.commit()

    @synchronized
    def record_served_impressions(self, request_id, surface, items, now, prune_before=None) -> None:
        """Persist final live order or an explicitly marked counterfactual replay.

        ``items`` are dictionaries containing lane/key/rank plus optional mode_id, score and
        provenance. The request/rank uniqueness key makes an HTMX retry idempotent. Instrumentation
        is fail-open at route call sites and bounded by ``prune_before``.
        """
        rows = []
        for item in items:
            key = item.get("identity_key") or item.get("key")
            if not key:
                continue
            rows.append((request_id, surface, item.get("lane") or "", item.get("mode_id"), key,
                         int(item["rank"]), item.get("score"), item.get("model_version") or "unknown",
                         json.dumps(item.get("provenance") or {}, sort_keys=True), now))
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO rec_served_impressions("
                "request_id,surface,lane,mode_id,identity_key,rank,score,model_version,provenance,served_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        if prune_before is not None:
            self.conn.execute("DELETE FROM rec_served_impressions WHERE served_at < ?", (prune_before,))
            self.conn.execute("DELETE FROM rec_response_events WHERE at < ?", (prune_before,))
            self.conn.execute("DELETE FROM rec_serving_policies WHERE created_at < ? AND model_version NOT IN "
                              "(SELECT DISTINCT model_version FROM rec_served_impressions) "
                              "AND model_version NOT IN (SELECT json_extract(provenance,'$.pool_policy') "
                              "FROM rec_served_impressions WHERE json_extract(provenance,'$.pool_policy') IS NOT NULL)",
                              (prune_before,))
        self.conn.commit()

    @synchronized
    def recommendation_scorecard(self, start, end, outcome_window_h=24, min_impressions=100,
                                 impression_id=None, experiments_only=False) -> dict:
        """Attribute subsequent play/exit events to the nearest preceding rendered opportunity.

        The impression range is bounded to ``[start, end)``; outcomes may arrive for
        ``outcome_window_h`` after each impression. Ownership is resolved across all impressions
        before filtering report totals. Generated-playlist plays are useful response evidence but
        are reported separately and never counted as organic preference evidence. Historical plays
        without play-time policy count as responses, with unknown generated/organic provenance.
        """
        impressions = [dict(r) for r in self.conn.execute(
            "SELECT * FROM rec_served_impressions WHERE served_at>=? AND served_at<? "
            "AND COALESCE(json_extract(provenance,'$.policy_kind'),'live')='live' "
            "AND (?=0 OR json_extract(provenance,'$.experiment.comparison_id') IS NOT NULL) "
            "AND (? IS NULL OR id=?) ORDER BY served_at,id",
            (start, end, experiments_only, impression_id, impression_id)).fetchall()]
        horizon = max(0.0, float(outcome_window_h)) * 3600.0
        states = {r["id"]: {"played": False, "organic": False, "generated": False,
                             "unknown_provenance": False, "skip": False,
                             "completion": False, "open": False, "start": False,
                             "save": False, "promotion": False, "conversion": False,
                             "play_depth": None} for r in impressions}
        candidates = {r["id"]: json.loads(r["provenance"]).get("candidate", {}) for r in impressions}

        report_keys = {r["identity_key"] for r in impressions}

        def nearest(key, at):
            if key not in report_keys:
                return None
            owner = self.conn.execute(
                "SELECT id FROM rec_served_impressions WHERE identity_key=? "
                "AND COALESCE(json_extract(provenance,'$.policy_kind'),'live')='live' "
                "AND served_at>=? AND served_at<=? ORDER BY served_at DESC,id DESC LIMIT 1",
                (key, at - horizon, at)).fetchone()
            return owner["id"] if owner else None

        for play in self.conn.execute(
                "SELECT identity_key,played_at,is_generated FROM play_events "
                "WHERE played_at>=? AND played_at<=? ORDER BY played_at,id", (start, end + horizon)):
            state = states.get(nearest(play["identity_key"], play["played_at"]))
            if state is None:
                continue
            state["played"] = True
            if play["is_generated"] == 1:
                state["generated"] = True
            elif play["is_generated"] == 0:
                state["organic"] = True
            else:
                state["unknown_provenance"] = True

        for event in self.conn.execute(
                "SELECT * FROM rec_response_events WHERE at>=? AND at<=? ORDER BY at,id",
                (start, end + horizon)):
            owner = nearest(event["identity_key"], event["at"])
            state = states.get(owner)
            if state is None:
                continue
            state[event["kind"]] = True
            if (candidates[owner].get("ownership") == "cold"
                    and (event["kind"] == "promotion"
                         or event["kind"] == "save" and not event["is_generated"])):
                state["conversion"] = True

        # Resolve video-only events from the identity saved when the recommendation was offered.
        # Discovery GC and later catalog edits must not erase or reassign historical responses.
        # Older impressions have no snapshot, so retain their catalog lookup as a fallback.
        exits = self.conn.execute(
            "SELECT pe.video_id,pe.position,pe.duration,pe.at,"
            "COALESCE((SELECT identity_key FROM rec_served_impressions "
            "WHERE served_at>=pe.at-? AND served_at<=pe.at "
            "AND COALESCE(json_extract(provenance,'$.policy_kind'),'live')='live' "
            "AND json_extract(provenance,'$.candidate.video_id')=pe.video_id "
            "ORDER BY served_at DESC,id DESC LIMIT 1),"
            "(SELECT identity_key FROM tracks WHERE video_id=pe.video_id LIMIT 1),"
            "(SELECT identity_key FROM discovered_tracks WHERE video_id=pe.video_id LIMIT 1)) identity_key "
            "FROM player_events pe WHERE pe.kind IN ('track_exit','bye','ended') "
            "AND pe.at>=? AND pe.at<=? ORDER BY pe.at,pe.id", (horizon, start, end + horizon)).fetchall()
        for event in exits:
            if event["identity_key"] is None:
                continue
            state = states.get(nearest(event["identity_key"], event["at"]))
            if state is None:
                continue
            outcome = classify_exit(event["position"], event["duration"])
            if outcome in ("skip", "completion"):
                state[outcome] = True
            if event["duration"] and event["duration"] > 0 and event["position"] is not None:
                depth = max(0.0, min(1.0, event["position"] / event["duration"]))
                state["play_depth"] = max(state["play_depth"] or 0, depth)

        n = len(impressions)
        counts = {name: sum(1 for state in states.values() if state[name])
                  for name in ("played", "organic", "generated", "unknown_provenance", "skip", "completion",
                               "open", "start", "save", "promotion", "conversion")}

        def breakdown(label_for):
            groups = {}
            for imp in impressions:
                label = label_for(imp) or "unknown"
                bucket = groups.setdefault(label, {"impressions": 0, **{name: 0 for name in counts}})
                bucket.setdefault("_rows", []).append(imp)
                bucket["impressions"] += 1
                for outcome in counts:
                    bucket[outcome] += int(states[imp["id"]][outcome])
            for bucket in groups.values():
                rows = bucket.pop("_rows")
                metadata = [candidates[r["id"]] for r in rows]
                depths = [states[r["id"]]["play_depth"] for r in rows
                          if states[r["id"]]["play_depth"] is not None]
                ages = [max(0, r["served_at"] - candidates[r["id"]]["last_played"]) / 86400
                        for r in rows if candidates[r["id"]].get("last_played") is not None]
                cold = sum(c.get("ownership") == "cold" for c in metadata)
                unique = len({r["identity_key"] for r in rows})
                bucket["acceptance_rate"] = bucket["played"] / bucket["impressions"]
                bucket["completion_rate"] = bucket["completion"] / bucket["impressions"]
                bucket["skip_rate"] = bucket["skip"] / bucket["impressions"]
                bucket["cold_impressions"] = cold
                bucket["discovery_conversion_rate"] = bucket["conversion"] / cold if cold else None
                bucket["unique_items"] = unique
                bucket["repeat_rate"] = (len(rows) - unique) / len(rows)
                bucket["unique_artists"] = len({c["artist"] for c in metadata if c.get("artist")})
                bucket["unique_genres"] = len({c["genre"] for c in metadata if c.get("genre")})
                bucket["mean_play_depth"] = statistics.mean(depths) if depths else None
                bucket["play_depth_samples"] = len(depths)
                bucket["median_days_since_play"] = statistics.median(ages) if ages else None
                bucket["staleness_samples"] = len(ages)
                bucket["never_played"] = sum(c.get("last_played") is None and "last_played" in c for c in metadata)
                bucket["powered"] = bucket["impressions"] >= int(min_impressions)
                bucket["verdict"] = None
                clusters = {}
                opportunities = set()
                for row in rows:
                    experiment = json.loads(row["provenance"]).get("experiment") or {}
                    if not experiment:
                        continue
                    opportunities.add(experiment["comparison_id"])
                    cluster = clusters.setdefault(experiment["cluster"],
                        {"played": False, "completion": False, "skip": False})
                    for metric in cluster:
                        cluster[metric] |= states[row["id"]][metric]
                bucket["opportunities"] = len(opportunities)
                bucket["clusters"] = len(clusters)
                bucket["cluster_rates"] = {}
                bucket["cluster_ci95"] = {}
                for metric in ("played", "completion", "skip"):
                    hits = sum(cluster[metric] for cluster in clusters.values())
                    bucket["cluster_rates"][metric] = hits / len(clusters) if clusters else None
                    bucket["cluster_ci95"][metric] = _wilson(hits, len(clusters))
            return groups

        unique_keys = {r["identity_key"] for r in impressions}
        repeats = n - len(unique_keys)
        powered = n >= int(min_impressions)
        depths = [s["play_depth"] for s in states.values() if s["play_depth"] is not None]
        staleness = [max(0, r["served_at"] - candidates[r["id"]]["last_played"]) / 86400
                     for r in impressions if candidates[r["id"]].get("last_played") is not None]
        cold = sum(c.get("ownership") == "cold" for c in candidates.values())
        return {
            "start": start, "end": end, "outcome_window_h": outcome_window_h,
            "impressions": n, **counts,
            "acceptance_rate": counts["played"] / n if n else None,
            "organic_rate": counts["organic"] / n if n else None,
            "skip_rate": counts["skip"] / n if n else None,
            "completion_rate": counts["completion"] / n if n else None,
            # Acceptance is the north star because a live play is the broadest reliable response
            # sensor today. Completion and skip are guardrails; organic_rate is evidence suitable
            # for taste learning, while generated response is product efficacy only.
            "north_star": "rendered_to_played",
            "guardrails": ["skip_rate", "completion_rate", "repeat_rate", "unique_artists", "unique_genres"],
            "open_rate": counts["open"] / n if n else None,
            "save_rate": counts["save"] / n if n else None,
            "cold_impressions": cold,
            "discovery_conversion_rate": counts["conversion"] / cold if cold else None,
            "mean_play_depth": statistics.mean(depths) if depths else None,
            "play_depth_samples": len(depths),
            "unique_artists": len({c["artist"] for c in candidates.values() if c.get("artist")}),
            "unique_genres": len({c["genre"] for c in candidates.values() if c.get("genre")}),
            "median_days_since_play": statistics.median(staleness) if staleness else None,
            "staleness_samples": len(staleness),
            "never_played": sum(c.get("last_played") is None and "last_played" in c for c in candidates.values()),
            "unique_items": len(unique_keys), "repeat_rate": repeats / n if n else None,
            "by_surface": breakdown(lambda r: r["surface"]),
            "by_lane": breakdown(lambda r: r["lane"]),
            "by_mode": breakdown(lambda r: str(r["mode_id"]) if r["mode_id"] is not None else "unknown"),
            "by_ranker": breakdown(lambda r: json.loads(r["provenance"]).get("ranker")),
            "by_experiment": breakdown(lambda r:
                ((json.loads(r["provenance"]).get("experiment") or {}).get("cohort", "unknown") + ":" +
                 json.loads(r["provenance"]).get("ranker", "unknown"))),
            "by_ownership": breakdown(lambda r: candidates[r["id"]].get("ownership")),
            "by_metadata": {axis: breakdown(lambda r, axis=axis:
                ("present" if candidates[r["id"]]["coverage"].get(axis) else "missing")
                if "coverage" in candidates[r["id"]] else "unknown") for axis in ("genre", "year", "audio")},
            "min_impressions": int(min_impressions), "powered": powered,
            # A volume threshold can make a descriptive rate stable; it cannot manufacture a
            # counterfactual. #110's controlled comparison may add a verdict later.
            "verdict": None,
        }

    @synchronized
    def experiment_report(self, start, end, now):
        """Mature live cohorts only; a replay is a hypothetical ranking, never outcome evidence."""
        mature_end = min(end, now - 24 * 3600)
        report = self.recommendation_scorecard(start, max(start, mature_end), experiments_only=True)
        for bucket in report["by_experiment"].values():
            bucket["powered"] = bucket["clusters"] >= MIN_EXPERIMENT_CLUSTERS
        shadows = self.conn.execute(
            "SELECT json_extract(provenance,'$.ranker') ranker,COUNT(*) rows,"
            "COUNT(DISTINCT json_extract(provenance,'$.experiment.comparison_id')) opportunities "
            "FROM rec_served_impressions WHERE served_at>=? AND served_at<? "
            "AND json_extract(provenance,'$.policy_kind')='counterfactual' GROUP BY ranker",
            (start, end)).fetchall()
        return {"schema": 1, "start": start, "end": end, "mature_before": mature_end,
                "live": report["by_experiment"], "counterfactual": [dict(r) for r in shadows],
                "by_lane": report["by_lane"], "by_mode": report["by_mode"],
                "min_clusters_per_arm": MIN_EXPERIMENT_CLUSTERS,
                "uncertainty": "Wilson 95% intervals on epoch/mode clusters, repeated views pooled",
                "promotion_rule": "100 mature clusters per arm; disjoint acceptance intervals; "
                                  "no worse sustained-listening or skip intervals",
                "counterfactual_outcomes": None}

    # --- recipes: the exact theme/params a generated playlist was made from (legible + re-runnable) ---
    @synchronized
    def set_recipe(self, playlist_ytm, recipe, now=None) -> None:
        self.conn.execute(
            "INSERT INTO rec_recipes(playlist_ytm, recipe, created_at) VALUES (?,?,?) "
            "ON CONFLICT(playlist_ytm) DO UPDATE SET recipe=excluded.recipe, created_at=excluded.created_at",
            (playlist_ytm, json.dumps(recipe), now))
        self.conn.commit()

    @synchronized
    def get_recipe(self, playlist_ytm):
        row = self.conn.execute("SELECT recipe FROM rec_recipes WHERE playlist_ytm=?",
                                (playlist_ytm,)).fetchone()
        return json.loads(row["recipe"]) if row else None

    # --- cluster canvas: the full graph behind a saved cluster playlist, so it can be reopened and
    #     regrown a different way (#48). `state` is the client's serialized canvas blob, stored verbatim. ---
    @synchronized
    def set_cluster_canvas(self, playlist_ytm, state, now=None) -> None:
        self.conn.execute(
            "INSERT INTO cluster_canvas(playlist_ytm, state, created_at) VALUES (?,?,?) "
            "ON CONFLICT(playlist_ytm) DO UPDATE SET state=excluded.state, created_at=excluded.created_at",
            (playlist_ytm, state, now))
        self.conn.commit()

    @synchronized
    def get_cluster_canvas(self, playlist_ytm):
        row = self.conn.execute("SELECT state FROM cluster_canvas WHERE playlist_ytm=?",
                                (playlist_ytm,)).fetchone()
        return row["state"] if row else None

    @synchronized
    def delete_cluster_canvas(self, playlist_ytm) -> None:
        self.conn.execute("DELETE FROM cluster_canvas WHERE playlist_ytm=?", (playlist_ytm,))
        self.conn.commit()

    @synchronized
    def get_recipe_created_ats(self) -> dict:
        """{playlist_ytm: created_at} for every generated playlist, lets the playlists page order
        the Generated list newest-first. created_at may be None for older recipes."""
        return {r["playlist_ytm"]: r["created_at"]
                for r in self.conn.execute(
                    "SELECT playlist_ytm, created_at FROM rec_recipes").fetchall()}

    # --- persistent mood: count-capped (replaces time-windowed active_mood) ---
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
    def lane_impression_counts(self, since=None) -> dict:
        """Actual exposure counts from the unified ledger, excluding replay rows."""
        rows = self.conn.execute(
            "SELECT lane,COUNT(*) c FROM rec_served_impressions WHERE (? IS NULL OR served_at>=?) "
            "AND COALESCE(json_extract(provenance,'$.policy_kind'),'live')='live' GROUP BY lane",
            (since, since)).fetchall()
        return {r["lane"]: r["c"] for r in rows}

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

    # --- materialized proposals (rec worker writes, routes read last-good) ---
    @synchronized
    def put_proposals(self, surface, data, now=None) -> None:
        """Materialize a surface's proposals as JSON (the rec worker writes; routes read last-good)."""
        self.conn.execute(
            "INSERT INTO rec_proposals(surface, payload, built_at) VALUES (?,?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload, built_at=excluded.built_at",
            (surface, json.dumps(data), now))
        self.conn.commit()

    @synchronized
    def get_proposals(self, surface):
        """Last materialized proposals for a surface, or None if never built."""
        row = self.conn.execute(
            "SELECT payload FROM rec_proposals WHERE surface=?", (surface,)).fetchone()
        return json.loads(row["payload"]) if row else None

    # --- Last.fm similar-artist cache (14-day TTL) ---
    @synchronized
    def cached_similar(self, artist, now, ttl_days=14):
        """Cached Last.fm similar-artist list, or None if missing/expired."""
        row = self.conn.execute(
            "SELECT payload, fetched_at FROM rec_artist_similar WHERE artist=?", (artist,)).fetchone()
        if row is None or row["fetched_at"] is None or now - row["fetched_at"] > ttl_days * 86400:
            return None
        return json.loads(row["payload"])

    @synchronized
    def artist_similar_edges(self, artist) -> list:
        """Cached Last.fm similar pairs [[name, match], ...] for an artist, ignoring TTL ([] if none).
        Used by the #28 artist model's §C edge block, where a slightly-stale relatedness hint is fine
        (the model rebuilds periodically and new_artists keeps the cache warm)."""
        row = self.conn.execute(
            "SELECT payload FROM rec_artist_similar WHERE artist=?", (artist,)).fetchone()
        return json.loads(row["payload"]) if row and row["payload"] else []

    @synchronized
    def cache_similar(self, artist, pairs, now) -> None:
        self.conn.execute(
            "INSERT INTO rec_artist_similar(artist, payload, fetched_at) VALUES (?,?,?) "
            "ON CONFLICT(artist) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (artist, json.dumps(pairs), now))
        self.conn.commit()

    # --- graduation ledger (interaction-driven; no wall-clock decay) ---
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
    def recent_graduations(self, limit=50) -> list:
        """The most recent graduation events, newest-first: rows of
        {id, axis, source, score, factor, new_weight, created_at}."""
        return self.conn.execute(
            "SELECT id, axis, source, score, factor, new_weight, created_at FROM rec_grad_log "
            "ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()

    @synchronized
    def graduation_audit_rows(self) -> list:
        """Every graduation log row's (axis, source, factor), oldest-first. Powers the one-shot
        like-ratchet repair's entitlement audit (rec/repair.py): per-axis like-source counts and
        the non-like factor product that floors the correction."""
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
