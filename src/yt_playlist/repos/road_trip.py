"""RoadTripRepo: saved 'Road Trip' recipes (name, other-people's artists/genres, own-collection
genre blacklist, mine/theirs split, target length). Owns its own table (created lazily/idempotently,
same pattern as RecSurfaceRepo) since it's a first-class saved-config feature, not rec-serving state.
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
  blacklist_genres TEXT NOT NULL,
  target_minutes   INTEGER NOT NULL,
  last_playlist_id TEXT,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL
);
"""


class RoadTripRepo(Repo):
    def __init__(self, db):
        super().__init__(db)
        with self._lock:
            self.conn.executescript(_SCHEMA)

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
    def save_road_trip_recipe(self, recipe_id, name, own_pct, artists, genres, blacklist_genres,
                              target_minutes, now) -> int:
        """Insert (recipe_id is None) or update (recipe_id given) a recipe. Returns its id."""
        if recipe_id is None:
            cur = self.conn.execute(
                "INSERT INTO road_trip_recipes(name, own_pct, artists, genres, blacklist_genres, "
                "target_minutes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (name, own_pct, json.dumps(artists), json.dumps(genres),
                 json.dumps(blacklist_genres), target_minutes, now, now))
            self.conn.commit()
            return cur.lastrowid
        self.conn.execute(
            "UPDATE road_trip_recipes SET name=?, own_pct=?, artists=?, genres=?, "
            "blacklist_genres=?, target_minutes=?, updated_at=? WHERE id=?",
            (name, own_pct, json.dumps(artists), json.dumps(genres), json.dumps(blacklist_genres),
             target_minutes, now, recipe_id))
        self.conn.commit()
        return recipe_id

    @synchronized
    def delete_road_trip_recipe(self, recipe_id) -> None:
        self.conn.execute("DELETE FROM road_trip_recipes WHERE id=?", (recipe_id,))
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
                "target_minutes": r["target_minutes"], "last_playlist_id": r["last_playlist_id"],
                "created_at": r["created_at"], "updated_at": r["updated_at"]}
