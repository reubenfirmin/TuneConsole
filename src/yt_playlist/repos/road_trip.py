"""RoadTripRepo: saved 'Road Trip' recipes (name, other-people's artists/genres, own-collection
mine/theirs split, familiarity lean, target length) plus the live DRAFT each recipe
builds into. Owns its own tables (created lazily/idempotently, same pattern as RecSurfaceRepo) since
it's a first-class saved-config feature, not rec-serving state.

The draft is the on-screen playlist: its candidate pool, the slots currently picked, the slots the
user crossed out, and the per-party genre/era slider positions. It lives in the DB rather than in
memory so a reload (or --reload during development) doesn't throw away a mix that cost real network
time to assemble, and so 'Save to YouTube' materializes exactly the rows on screen. One draft per
recipe: building again replaces it.
"""
import json

from yt_playlist.repos.base import Repo, synchronized

_SCHEMA = """
CREATE TABLE IF NOT EXISTS road_trip_recipes (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  name             TEXT NOT NULL,
  own_pct          INTEGER NOT NULL,
  artists          TEXT NOT NULL,
  genres           TEXT NOT NULL,
  blacklist_genres TEXT NOT NULL,   -- legacy: the genre bars on the draft replaced it
  target_minutes   INTEGER NOT NULL,
  last_playlist_id TEXT,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS road_trip_drafts (
  recipe_id  INTEGER PRIMARY KEY,
  state      TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""

DEFAULT_FAMILIARITY_PCT = 50   # dead centre: favorites and deeper cuts weighted alike


class RoadTripRepo(Repo):
    def __init__(self, db):
        super().__init__(db)
        with self._lock:
            self.conn.executescript(_SCHEMA)
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(road_trip_recipes)")}
            if "familiarity_pct" not in cols:    # added with the familiarity slider
                self.conn.execute("ALTER TABLE road_trip_recipes ADD COLUMN familiarity_pct INTEGER "
                                  f"NOT NULL DEFAULT {DEFAULT_FAMILIARITY_PCT}")

    @synchronized
    def list_road_trip_recipes(self) -> list:
        rows = self.conn.execute(
            "SELECT * FROM road_trip_recipes ORDER BY updated_at DESC").fetchall()
        return [self._row(r) for r in rows]

    @synchronized
    def get_road_trip_recipe(self, recipe_id):
        row = self.conn.execute(
            "SELECT * FROM road_trip_recipes WHERE id=?", (recipe_id,)).fetchone()
        return self._row(row) if row else None

    @synchronized
    def save_road_trip_recipe(self, recipe_id, name, own_pct, artists, genres,
                              target_minutes, now, familiarity_pct=DEFAULT_FAMILIARITY_PCT,
                              blacklist_genres=None) -> int:
        """Insert (recipe_id is None) or update (recipe_id given) a recipe. Returns its id."""
        if recipe_id is None:
            cur = self.conn.execute(
                "INSERT INTO road_trip_recipes(name, own_pct, artists, genres, blacklist_genres, "
                "target_minutes, familiarity_pct, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (name, own_pct, json.dumps(artists), json.dumps(genres),
                 json.dumps(blacklist_genres or []), target_minutes, familiarity_pct, now, now))
            self.conn.commit()
            return cur.lastrowid
        self.conn.execute(
            "UPDATE road_trip_recipes SET name=?, own_pct=?, artists=?, genres=?, blacklist_genres=?, "
            "target_minutes=?, familiarity_pct=?, updated_at=? WHERE id=?",
            (name, own_pct, json.dumps(artists), json.dumps(genres),
             json.dumps(blacklist_genres or []), target_minutes, familiarity_pct, now, recipe_id))
        self.conn.commit()
        return recipe_id

    @synchronized
    def delete_road_trip_recipe(self, recipe_id) -> None:
        self.conn.execute("DELETE FROM road_trip_recipes WHERE id=?", (recipe_id,))
        self.conn.execute("DELETE FROM road_trip_drafts WHERE recipe_id=?", (recipe_id,))
        self.conn.commit()

    @synchronized
    def get_road_trip_draft(self, recipe_id):
        """The recipe's live on-screen draft (the state dict rec/road_trip.py builds), or None."""
        row = self.conn.execute(
            "SELECT state FROM road_trip_drafts WHERE recipe_id=?", (recipe_id,)).fetchone()
        return json.loads(row["state"]) if row else None

    @synchronized
    def latest_road_trip_draft(self):
        """{recipe_id, state} for the most recently touched draft, or None. What the Road Trip page
        reopens with, so a mix you were curating survives a reload."""
        row = self.conn.execute(
            "SELECT d.recipe_id, d.state FROM road_trip_drafts d "
            "JOIN road_trip_recipes r ON r.id=d.recipe_id ORDER BY d.updated_at DESC "
            "LIMIT 1").fetchone()
        return {"recipe_id": row["recipe_id"], "state": json.loads(row["state"])} if row else None

    @synchronized
    def save_road_trip_draft(self, recipe_id, state, now) -> None:
        self.conn.execute(
            "INSERT INTO road_trip_drafts(recipe_id, state, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(recipe_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (recipe_id, json.dumps(state), now))
        self.conn.commit()

    @synchronized
    def delete_road_trip_draft(self, recipe_id) -> None:
        self.conn.execute("DELETE FROM road_trip_drafts WHERE recipe_id=?", (recipe_id,))
        self.conn.commit()

    @synchronized
    def set_road_trip_last_playlist(self, recipe_id, playlist_ytm) -> None:
        self.conn.execute(
            "UPDATE road_trip_recipes SET last_playlist_id=? WHERE id=?", (playlist_ytm, recipe_id))
        self.conn.commit()

    def _row(self, r) -> dict:
        return {"id": r["id"], "name": r["name"], "own_pct": r["own_pct"],
                "artists": json.loads(r["artists"]), "genres": json.loads(r["genres"]),
                "blacklist_genres": json.loads(r["blacklist_genres"]),
                "target_minutes": r["target_minutes"],
                "familiarity_pct": r["familiarity_pct"],
                "last_playlist_id": r["last_playlist_id"],
                "created_at": r["created_at"], "updated_at": r["updated_at"]}
