"""RecSurfaceRepo: the recommendation *serving* surfaces: impression counts (anti-staleness
erosion), materialized last-good proposals, and the Last.fm similar-artist cache.

Owns its own tables (created lazily/idempotently) since they're rec-internal serving state.
"""
import json

from yt_playlist.repos.base import Repo, synchronized

MOOD_EVENT_CAP = 200   # bound the rec_mood table (count, not age); this repo owns that table

_SCHEMA = """
-- per-(surface, item) view counts + last_shown: anti-staleness erosion and per-card rotation
CREATE TABLE IF NOT EXISTS rec_impressions (
  surface TEXT NOT NULL,                  -- 'for_you' | 'explore' | 'suggest'
  item_key TEXT NOT NULL,
  views INTEGER NOT NULL DEFAULT 0,       -- times shown (debounced), for anti-staleness erosion
  last_shown REAL,                        -- drives the recycle cooldown
  PRIMARY KEY (surface, item_key)
);
-- materialized last-good proposal payloads per surface (rec worker writes, routes read)
CREATE TABLE IF NOT EXISTS rec_proposals (
  surface TEXT PRIMARY KEY,               -- 'fresh_songs' | 'discover'
  payload TEXT NOT NULL,                  -- JSON, materialized by the rec worker (last-good serving)
  built_at REAL
);
-- Last.fm similar-artist cache (14-day TTL); also the #28 artist model's edge source
CREATE TABLE IF NOT EXISTS rec_artist_similar (
  artist TEXT PRIMARY KEY,                -- anchor artist (display name)
  payload TEXT NOT NULL,                  -- JSON [[name, match], ...] from Last.fm
  fetched_at REAL
);
-- count-capped log of mood feedback events (the transient model's mood seed)
CREATE TABLE IF NOT EXISTS rec_mood (
  created_at REAL NOT NULL,               -- when the mood feedback was given (drives time-decay)
  direction INTEGER NOT NULL,             -- +1 = more of this vibe, -1 = not my mood
  keys TEXT NOT NULL                      -- JSON list of the playlist's track identity_keys (the seed)
);
-- the theme/params a generated playlist was built from (legible + re-runnable)
CREATE TABLE IF NOT EXISTS rec_recipes (
  playlist_ytm TEXT PRIMARY KEY,          -- the generated playlist's YouTube id
  recipe TEXT NOT NULL,                   -- JSON: the rolled theme + params + dj seed + version
  created_at REAL
);
-- the full canvas blob behind a saved cluster playlist, so it can be reopened and regrown (#48)
CREATE TABLE IF NOT EXISTS cluster_canvas (
  playlist_ytm TEXT PRIMARY KEY,          -- the generated playlist's YouTube id (#48: reopen its cluster)
  state TEXT NOT NULL,                     -- JSON: the full canvas blob (nodes, trunk, seeds, filter, view)
  created_at REAL
);
-- graduation ledger: persistent signed running totals (the transient -> permanent funnel)
CREATE TABLE IF NOT EXISTS rec_theme (
  facet      TEXT PRIMARY KEY,             -- 'genre:<fam>' | 'era:<decade>' | 'artist:<name>'
  score      REAL NOT NULL,                -- persistent signed running total (interaction-driven, no time decay)
  updated_at REAL NOT NULL                 -- bookkeeping only
);
-- per-axis once-per-UTC-day stamp: the play-exposure graduation idempotency guard
CREATE TABLE IF NOT EXISTS rec_play_grad (
  axis               TEXT PRIMARY KEY,      -- facet token, e.g. 'genre:techno'
  last_graduated_day TEXT                   -- UTC YYYY-MM-DD the play-exposure funnel last graduated this axis
);
-- append-only graduation instrumentation: one row per threshold-crossing nudge (model-health panel)
CREATE TABLE IF NOT EXISTS rec_grad_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  axis       TEXT NOT NULL,                 -- the facet that graduated, e.g. 'genre:techno'
  source     TEXT NOT NULL,                 -- the signal that drove it: 'play' | 'slider' | 'vibe' | 'like' | ...
  score      REAL NOT NULL,                 -- the ledger value at the crossing (what tripped THEME_THRESHOLD)
  factor     REAL NOT NULL,                 -- the nudge multiplier applied (graduate_up / graduate_down)
  new_weight REAL NOT NULL,                 -- the resulting permanent weight after the nudge
  created_at REAL NOT NULL
);
"""


class RecSurfaceRepo(Repo):
    def __init__(self, db):
        super().__init__(db)
        with self._lock:
            self.conn.executescript(_SCHEMA)   # this DAO owns its tables (idempotent)

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

    # --- #87 lane impressions: pure instrumentation for a future lane bandit (rec_lane_impressions,
    #     defined in core.store SCHEMA since it's a plain append-only log, not surface-owned state) ---
    @synchronized
    def record_lane_impressions(self, items, now, prune_before=None) -> None:
        """Log which lane served each rendered item: items is [(lane, identity_key), ...]. One
        executemany insert; when prune_before is set, rows with at < prune_before are deleted in the
        same transaction. No-op on an empty items list. Read by lane_impression_counts."""
        items = list(items)
        if not items:
            return
        self.conn.executemany(
            "INSERT INTO rec_lane_impressions(lane, identity_key, at) VALUES (?,?,?)",
            [(lane, key, now) for lane, key in items])
        if prune_before is not None:
            self.conn.execute("DELETE FROM rec_lane_impressions WHERE at < ?", (prune_before,))
        self.conn.commit()

    @synchronized
    def lane_impression_counts(self, since=None) -> dict:
        """{lane: count} of logged impressions, optionally restricted to at >= since. The future
        lane-bandit's read side; also proof the data lands."""
        if since is None:
            rows = self.conn.execute(
                "SELECT lane, COUNT(*) c FROM rec_lane_impressions GROUP BY lane").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT lane, COUNT(*) c FROM rec_lane_impressions WHERE at >= ? GROUP BY lane",
                (since,)).fetchall()
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
        {axis, source, score, factor, new_weight, created_at}."""
        return self.conn.execute(
            "SELECT axis, source, score, factor, new_weight, created_at FROM rec_grad_log "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

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
