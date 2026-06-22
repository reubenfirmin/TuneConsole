"""RecSurfaceRepo — the recommendation *serving* surfaces: impression counts (anti-staleness
erosion), materialized last-good proposals, and the Last.fm similar-artist cache.

Owns its own tables (created lazily/idempotently) since they're rec-internal serving state.
"""
import json

from yt_playlist.repos.base import Repo, synchronized

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rec_impressions (
  surface TEXT NOT NULL,                  -- 'for_you' | 'explore' | 'suggest'
  item_key TEXT NOT NULL,
  views INTEGER NOT NULL DEFAULT 0,       -- times shown (debounced), for anti-staleness erosion
  last_shown REAL,                        -- drives the recycle cooldown
  PRIMARY KEY (surface, item_key)
);
CREATE TABLE IF NOT EXISTS rec_proposals (
  surface TEXT PRIMARY KEY,               -- 'auto_playlists' | 'discover'
  payload TEXT NOT NULL,                  -- JSON, materialized by the rec worker (last-good serving)
  built_at REAL
);
CREATE TABLE IF NOT EXISTS rec_artist_similar (
  artist TEXT PRIMARY KEY,                -- anchor artist (display name)
  payload TEXT NOT NULL,                  -- JSON [[name, match], ...] from Last.fm
  fetched_at REAL
);
CREATE TABLE IF NOT EXISTS rec_mood (
  created_at REAL NOT NULL,               -- when the mood feedback was given (drives time-decay)
  direction INTEGER NOT NULL,             -- +1 = more of this vibe, -1 = not my mood
  keys TEXT NOT NULL                      -- JSON list of the playlist's track identity_keys (the seed)
);
CREATE TABLE IF NOT EXISTS rec_recipes (
  playlist_ytm TEXT PRIMARY KEY,          -- the generated playlist's YouTube id
  recipe TEXT NOT NULL,                   -- JSON: the rolled theme + params + dj seed + version
  created_at REAL
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

    # --- transient mood: short-lived, decaying tilt on the recommendation lanes (NOT permanent taste) ---
    @synchronized
    def record_mood(self, keys, direction, now) -> None:
        """Log a mood signal: the seed tracks (a generated playlist's) and whether the user wants
        more (+1) or less (-1) of that vibe right now. Read back by active_mood within a short window."""
        self.conn.execute("INSERT INTO rec_mood(created_at, direction, keys) VALUES (?,?,?)",
                          (now, int(direction), json.dumps(list(keys))))
        self.conn.commit()

    @synchronized
    def active_mood(self, now, window_h=8) -> list:
        """Recent mood events within the window: [(created_at, direction, [keys])]. Older events have
        already decayed to nothing, so they're dropped (and pruned) rather than returned."""
        cutoff = now - window_h * 3600
        self.conn.execute("DELETE FROM rec_mood WHERE created_at < ?", (cutoff,))
        self.conn.commit()
        return [(r["created_at"], r["direction"], json.loads(r["keys"]))
                for r in self.conn.execute(
                    "SELECT created_at, direction, keys FROM rec_mood WHERE created_at >= ?", (cutoff,))]

    # --- per-card rotation (the rec_impressions table, surface='card') ---
    @synchronized
    def bump_card_view(self, card, now) -> int:
        """Count one real view of a Home card (one row per card, surface='card') and return its new
        total. Drives per-card rotation: a card holds its content for erosion_view_cap views, then
        epoch = (views-1)//cap advances and it regenerates. Ticked once per genuine Home visit —
        never on steer/stance previews — so tuning your taste model doesn't churn the cards."""
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
        """Current view total for a Home card (0 if never shown) — read-only, so previews and
        re-renders can compute the card's rotation epoch without advancing it."""
        row = self.conn.execute(
            "SELECT views FROM rec_impressions WHERE surface='card' AND item_key=?", (card,)).fetchone()
        return row["views"] if row else 0

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
    def cache_similar(self, artist, pairs, now) -> None:
        self.conn.execute(
            "INSERT INTO rec_artist_similar(artist, payload, fetched_at) VALUES (?,?,?) "
            "ON CONFLICT(artist) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (artist, json.dumps(pairs), now))
        self.conn.commit()
