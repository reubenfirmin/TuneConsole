"""Saved playlist recipes and the Clusters canvases that produced them."""
import json

from yt_playlist.repos.base import Repo, synchronized, snapshot_read
from yt_playlist.core import playlist_recipe_state


class PlaylistRecipesRepo(Repo):
    @synchronized
    def set_recipe(self, playlist_ytm, recipe, now=None) -> None:
        from yt_playlist.core.playlist_scopes import ensure
        playlist_ytm = ensure(self.conn,playlist_ytm)
        if playlist_recipe_state.available(self.conn):
            with self.conn:
                playlist_recipe_state.save(self.conn,playlist_ytm,recipe,now)
            return
        self.conn.execute(
            "INSERT INTO rec_recipes(playlist_ytm, recipe, created_at) VALUES (?,?,?) "
            "ON CONFLICT(playlist_ytm) DO UPDATE SET recipe=excluded.recipe, created_at=excluded.created_at",
            (playlist_ytm, json.dumps(recipe), now))
        self.conn.commit()

    @snapshot_read
    def get_recipe(self, playlist_ytm):
        from yt_playlist.core.playlist_scopes import resolve
        playlist_ytm = resolve(self.conn,playlist_ytm)
        if playlist_recipe_state.available(self.conn):
            return playlist_recipe_state.load(self.conn,playlist_ytm)
        from yt_playlist.core.preparation.collection_document_publication import OWNERS
        owner = OWNERS['recipes']
        stage = owner.selected(self.conn)
        if stage is not None:
            raw = owner.value(self.conn, stage, playlist_ytm)
        else:
            row = self.conn.execute("SELECT recipe FROM rec_recipes WHERE playlist_ytm=?",
                                    (playlist_ytm,)).fetchone()
            raw = row['recipe'] if row else None
        from yt_playlist.core.preparation.append_publication import apply_tv
        return apply_tv(self.conn, playlist_ytm, json.loads(raw) if raw is not None else None)

    @snapshot_read
    def recent_video_playlist_refs(self, limit=20):
        """Bounded saved video outputs for rebuilding a missing disposable pool."""
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('Video recipe lookup requires a limit from 1 to 20')
        if playlist_recipe_state.available(self.conn):
            return [row[0] for row in self.conn.execute(
                "SELECT playlist_scope_id FROM rec_recipes WHERE media='video' "
                'ORDER BY created_at DESC,playlist_scope_id LIMIT ?', (limit,))]
        from yt_playlist.core.preparation.collection_document_publication import OWNERS
        owner = OWNERS['recipes']
        stage = owner.selected(self.conn)
        if stage is not None:
            return [row[0] for row in self.conn.execute(
                'SELECT m.source FROM '+owner.table('map')+' m JOIN '+owner.header+
                ' g ON g.source=m.source AND g.preparation_revision=m.generation '
                "WHERE m.stage=? AND g.media='video' ORDER BY g.created_at DESC,m.source LIMIT ?",
                (stage, limit))]
        return [row[0] for row in self.conn.execute(
            'SELECT playlist_ytm FROM rec_recipes WHERE CASE WHEN json_valid(recipe) '
            "THEN json_extract(recipe,'$.media') END='video' "
            'ORDER BY created_at DESC,playlist_ytm LIMIT ?', (limit,))]

    # --- cluster canvas: the full graph behind a saved cluster playlist, so it can be reopened and
    #     regrown a different way (#48). `state` is the client's serialized canvas blob, stored verbatim. ---
    @synchronized
    def set_cluster_canvas(self, playlist_ytm, state, now=None) -> None:
        from yt_playlist.core.playlist_scopes import ensure
        playlist_ytm = ensure(self.conn,playlist_ytm)
        if playlist_recipe_state.available(self.conn):
            with self.conn:
                playlist_recipe_state.save_canvas(self.conn,playlist_ytm,state,now)
            return
        self.conn.execute(
            "INSERT INTO cluster_canvas(playlist_ytm, state, created_at) VALUES (?,?,?) "
            "ON CONFLICT(playlist_ytm) DO UPDATE SET state=excluded.state, created_at=excluded.created_at",
            (playlist_ytm, state, now))
        self.conn.commit()

    @snapshot_read
    def get_cluster_canvas(self, playlist_ytm):
        from yt_playlist.core.playlist_scopes import resolve
        playlist_ytm = resolve(self.conn,playlist_ytm)
        if playlist_recipe_state.available(self.conn):
            return playlist_recipe_state.load_canvas(self.conn,playlist_ytm)
        from yt_playlist.core.preparation.collection_document_publication import OWNERS
        owner = OWNERS['canvases']
        stage = owner.selected(self.conn)
        if stage is not None:
            return owner.value(self.conn, stage, playlist_ytm)
        row = self.conn.execute("SELECT state FROM cluster_canvas WHERE playlist_ytm=?",
                                (playlist_ytm,)).fetchone()
        return row["state"] if row else None

    @synchronized
    def delete_cluster_canvas(self, playlist_ytm) -> None:
        from yt_playlist.core.playlist_scopes import resolve
        playlist_ytm = resolve(self.conn,playlist_ytm)
        if playlist_recipe_state.available(self.conn):
            with self.conn:
                self.conn.execute('DELETE FROM cluster_canvas WHERE playlist_scope_id=?',(playlist_ytm,))
            return
        self.conn.execute("DELETE FROM cluster_canvas WHERE playlist_ytm=?", (playlist_ytm,))
        self.conn.commit()

    @snapshot_read
    def get_recipe_created_ats(self) -> dict:
        """{playlist_ytm: created_at} for every generated playlist, lets the playlists page order
        the Generated list newest-first. created_at may be None for older recipes."""
        native = playlist_recipe_state.available(self.conn)
        if not native:
            from yt_playlist.core.preparation.collection_document_publication import OWNERS
            owner = OWNERS['recipes']
            stage = owner.selected(self.conn)
            if stage is not None:
                return {row[0]:row[1] for row in self.conn.execute('SELECT m.source,g.created_at FROM '+
                    owner.table('map')+' m JOIN '+owner.header+
                    ' g ON g.source=m.source AND g.preparation_revision=m.generation WHERE m.stage=?', (stage,))}
        column = 'playlist_scope_id' if native else 'playlist_ytm'
        return {r[column]: r["created_at"]
                for r in self.conn.execute(
                    f"SELECT {column}, created_at FROM rec_recipes").fetchall()}

