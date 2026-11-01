"""RoadTripRepo: saved 'Road Trip' recipes (name, other-people's artists/genres, own-collection
mine/theirs split, familiarity lean, target length) plus the live DRAFT each recipe
builds into. Numbered migrations create its tables before repository use.

The draft is the on-screen playlist: its candidate pool, the slots currently picked, the slots the
user crossed out, and the per-party genre/era slider positions. It lives in the DB rather than in
memory so a reload (or --reload during development) doesn't throw away a mix that cost real network
time to assemble, and so 'Save to YouTube' materializes exactly the rows on screen. One draft per
recipe: building again replaces it.
"""
import json

from yt_playlist.core.road_trip_contract import DEFAULT_FAMILIARITY_PCT, validate_recipe_values
from yt_playlist.core import recipe_memberships, road_trip_draft_storage
from yt_playlist.repos.base import Repo, synchronized, snapshot_read


class RoadTripRepo(Repo):
    @snapshot_read
    def list_road_trip_recipes(self) -> list:
        rows = self.conn.execute(
            'SELECT '+self._recipe_columns()+' FROM road_trip_recipes ORDER BY updated_at DESC').fetchall()
        return [self._row(r) for r in rows]

    @snapshot_read
    def get_road_trip_recipe(self, recipe_id):
        row = self.conn.execute(
            'SELECT '+self._recipe_columns()+' FROM road_trip_recipes WHERE id=?', (recipe_id,)).fetchone()
        return self._row(row) if row else None

    @synchronized
    def save_road_trip_recipe(self, recipe_id, name, own_pct, artists, genres,
                              target_minutes, now, familiarity_pct=DEFAULT_FAMILIARITY_PCT,
                              blacklist_genres=None) -> int:
        """Insert (recipe_id is None) or update (recipe_id given) a recipe. Returns its id."""
        validate_recipe_values(own_pct, familiarity_pct, target_minutes, now)
        if recipe_memberships.available(self.conn):
            with self.conn:
                if recipe_id is None:
                    recipe_id = self.conn.execute('INSERT INTO road_trip_recipes(name,own_pct,target_minutes,'
                        'familiarity_pct,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                        (name,own_pct,target_minutes,familiarity_pct,now,now)).lastrowid
                else:
                    changed = self.conn.execute('UPDATE road_trip_recipes SET name=?,own_pct=?,target_minutes=?,'
                        'familiarity_pct=?,updated_at=? WHERE id=?',
                        (name,own_pct,target_minutes,familiarity_pct,now,recipe_id)).rowcount
                    if not changed:
                        raise ValueError('Unknown recipe')
                recipe_memberships.replace(self.conn,recipe_id,artists,genres,blacklist_genres or [])
            return recipe_id
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

    @snapshot_read
    def get_road_trip_draft(self, recipe_id):
        """The recipe's live on-screen draft (the state dict rec/road_trip.py builds), or None."""
        if road_trip_draft_storage.available(self.conn):
            return road_trip_draft_storage.get(self.conn,recipe_id)
        from yt_playlist.core.preparation.draft_publication import OWNER
        stage = OWNER.selected(self.conn)
        if stage is not None:
            raw = OWNER.value(self.conn, stage, recipe_id)
            return json.loads(raw) if raw is not None else None
        row = self.conn.execute(
            "SELECT state FROM road_trip_drafts WHERE recipe_id=?", (recipe_id,)).fetchone()
        return json.loads(row["state"]) if row else None

    @snapshot_read
    def latest_road_trip_draft(self):
        """{recipe_id, state} for the most recently touched draft, or None. What the Road Trip page
        reopens with, so a mix you were curating survives a reload."""
        if road_trip_draft_storage.available(self.conn):
            return road_trip_draft_storage.get(self.conn,latest=True)
        from yt_playlist.core.preparation.draft_publication import OWNER
        stage = OWNER.selected(self.conn)
        if stage is not None:
            row = self.conn.execute('SELECT g.recipe_id FROM '+OWNER.table('map')+' m JOIN '+OWNER.header+
                ' g ON g.recipe_id=m.source AND g.generation=m.generation JOIN road_trip_recipes r ON r.id=g.recipe_id '
                'WHERE m.stage=? ORDER BY g.updated_at DESC LIMIT 1', (stage,)).fetchone()
            return {'recipe_id':row[0], 'state':json.loads(OWNER.value(self.conn, stage, row[0]))} if row else None
        row = self.conn.execute(
            "SELECT d.recipe_id, d.state FROM road_trip_drafts d "
            "JOIN road_trip_recipes r ON r.id=d.recipe_id ORDER BY d.updated_at DESC "
            "LIMIT 1").fetchone()
        return {"recipe_id": row["recipe_id"], "state": json.loads(row["state"])} if row else None

    @synchronized
    def save_road_trip_draft(self, recipe_id, state, now) -> None:
        if road_trip_draft_storage.available(self.conn):
            with self.conn:
                road_trip_draft_storage.save(self.conn,recipe_id,state,now)
            return
        self.conn.execute(
            "INSERT INTO road_trip_drafts(recipe_id, state, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(recipe_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (recipe_id, json.dumps(state), now))
        self.conn.commit()

    @synchronized
    def replace_road_trip_draft(self, recipe_id, state, now, *, expected_revision) -> bool:
        """Publish worker results only while their exact input draft still exists."""
        if road_trip_draft_storage.available(self.conn):
            with self.conn:
                return road_trip_draft_storage.save(self.conn,recipe_id,state,now,
                    expected_revision=expected_revision,conditional=True)
        with self.conn.atomic():
            current = self.get_road_trip_draft(recipe_id)
            if current is None or current.get('revision') != expected_revision:
                return False
            self.save_road_trip_draft(recipe_id, state, now)
            return True

    @synchronized
    def delete_road_trip_draft(self, recipe_id) -> None:
        self.conn.execute("DELETE FROM road_trip_drafts WHERE recipe_id=?", (recipe_id,))
        self.conn.commit()

    @synchronized
    def set_road_trip_last_playlist(self, recipe_id, playlist_ytm) -> None:
        self.conn.execute(
            "UPDATE road_trip_recipes SET last_playlist_id=? WHERE id=?", (playlist_ytm, recipe_id))
        self.conn.commit()

    def _recipe_columns(self):
        from yt_playlist.core.preparation.recipe_publication import OWNER
        columns = 'id,name,own_pct,target_minutes,familiarity_pct,last_playlist_id,created_at,updated_at'
        if not recipe_memberships.available(self.conn) and OWNER.selected(self.conn) is None:
            columns += ',artists,genres,blacklist_genres'
        return columns

    def _row(self, r) -> dict:
        from yt_playlist.core.preparation import recipe_publication
        if recipe_memberships.available(self.conn):
            selections = recipe_memberships.read(self.conn,r['id'])
        else:
            selections = recipe_publication.read(self.conn, r['id'])
            if selections is None:
                selections = {field:json.loads(r[field]) for field in ('artists','genres','blacklist_genres')}
        return {"id": r["id"], "name": r["name"], "own_pct": r["own_pct"], **selections,
                "target_minutes": r["target_minutes"],
                "familiarity_pct": r["familiarity_pct"],
                "last_playlist_id": r["last_playlist_id"],
                "created_at": r["created_at"], "updated_at": r["updated_at"]}
