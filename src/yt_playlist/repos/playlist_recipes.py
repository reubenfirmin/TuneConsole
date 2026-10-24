"""Saved playlist recipes and the Clusters canvases that produced them."""
import json

from yt_playlist.repos.base import Repo, synchronized


class PlaylistRecipesRepo(Repo):
    @synchronized
    def set_recipe(self, playlist_ytm, recipe, now=None) -> None:
        from yt_playlist.core.playlist_scopes import ensure
        playlist_ytm = ensure(self.conn,playlist_ytm)
        self.conn.execute(
            "INSERT INTO rec_recipes(playlist_ytm, recipe, created_at) VALUES (?,?,?) "
            "ON CONFLICT(playlist_ytm) DO UPDATE SET recipe=excluded.recipe, created_at=excluded.created_at",
            (playlist_ytm, json.dumps(recipe), now))
        self.conn.commit()

    @synchronized
    def get_recipe(self, playlist_ytm):
        from yt_playlist.core.playlist_scopes import resolve
        playlist_ytm = resolve(self.conn,playlist_ytm)
        row = self.conn.execute("SELECT recipe FROM rec_recipes WHERE playlist_ytm=?",
                                (playlist_ytm,)).fetchone()
        return json.loads(row["recipe"]) if row else None

    # --- cluster canvas: the full graph behind a saved cluster playlist, so it can be reopened and
    #     regrown a different way (#48). `state` is the client's serialized canvas blob, stored verbatim. ---
    @synchronized
    def set_cluster_canvas(self, playlist_ytm, state, now=None) -> None:
        from yt_playlist.core.playlist_scopes import ensure
        playlist_ytm = ensure(self.conn,playlist_ytm)
        self.conn.execute(
            "INSERT INTO cluster_canvas(playlist_ytm, state, created_at) VALUES (?,?,?) "
            "ON CONFLICT(playlist_ytm) DO UPDATE SET state=excluded.state, created_at=excluded.created_at",
            (playlist_ytm, state, now))
        self.conn.commit()

    @synchronized
    def get_cluster_canvas(self, playlist_ytm):
        from yt_playlist.core.playlist_scopes import resolve
        playlist_ytm = resolve(self.conn,playlist_ytm)
        row = self.conn.execute("SELECT state FROM cluster_canvas WHERE playlist_ytm=?",
                                (playlist_ytm,)).fetchone()
        return row["state"] if row else None

    @synchronized
    def delete_cluster_canvas(self, playlist_ytm) -> None:
        from yt_playlist.core.playlist_scopes import resolve
        playlist_ytm = resolve(self.conn,playlist_ytm)
        self.conn.execute("DELETE FROM cluster_canvas WHERE playlist_ytm=?", (playlist_ytm,))
        self.conn.commit()

    @synchronized
    def get_recipe_created_ats(self) -> dict:
        """{playlist_ytm: created_at} for every generated playlist, lets the playlists page order
        the Generated list newest-first. created_at may be None for older recipes."""
        return {r["playlist_ytm"]: r["created_at"]
                for r in self.conn.execute(
                    "SELECT playlist_ytm, created_at FROM rec_recipes").fetchall()}

