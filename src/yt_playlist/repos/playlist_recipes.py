"""SQL owners and detached file references for saved playlist documents."""
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.core import append_work, playlist_recipe_state, playlist_scopes
from yt_playlist.repos import collection_file_writes


class PlaylistRecipesRepo(Repo):
    @synchronized
    def reserve_file(self, kind, owner, integrity):
        with self.conn.atomic():
            return collection_file_writes.reserve(self.conn, kind, owner, integrity)

    @synchronized
    def document_owner(self, name, scope):
        with self.conn.atomic():
            scope = playlist_scopes.ensure(self.conn, scope)
            identity = playlist_recipe_state.reserve_identity(self.conn, scope, playlist_recipe_state.PREFIXES[name])
        return {'scope': scope, 'id': identity}

    @lookup_read
    def check_append(self, scope, append):
        append_work.check(self.conn, scope, append)

    @synchronized
    def publish_recipe(self, owner, revision, value, facets, reference, append, now, *, file_id):
        identity = owner['id']
        with self.conn.atomic():
            collection_file_writes.complete(self.conn, file_id, 'playlist-recipe', identity, reference)
            fields = playlist_recipe_state.facets(self.conn, value, facets)
            playlist_recipe_state.publish(self.conn, owner['scope'], 'recipe', identity, revision, now, reference, fields)
            append_work.install(self.conn, owner['scope'], append)

    @synchronized
    def publish_canvas(self, owner, revision, reference, now, *, file_id):
        with self.conn.atomic():
            collection_file_writes.complete(self.conn, file_id, 'playlist-canvas', owner['id'], reference)
            playlist_recipe_state.publish(self.conn, owner['scope'], 'canvas', owner['id'], revision, now, reference)

    @snapshot_read
    def document_facts(self, name, scope):
        scope = playlist_scopes.resolve(self.conn, scope)
        row = playlist_recipe_state.header(self.conn, scope, playlist_recipe_state.PREFIXES[name])
        if row is None:
            return None
        facts = {'header': dict(row)}
        if name == 'recipes':
            facts['facets'] = playlist_recipe_state.current_facets(self.conn, facts['header'].pop('facets'))
            facts['appends'] = append_work.entries(self.conn, scope)
        return facts

    @snapshot_read
    def recent_video_playlist_refs(self, limit=20):
        return [row[0] for row in self.conn.execute("SELECT id FROM playlist_scopes WHERE recipe_media='video' "
            'AND recipe_revision IS NOT NULL ORDER BY recipe_created_at DESC,id LIMIT ?', (limit,))]

    @synchronized
    def delete_cluster_canvas(self, scope):
        scope = playlist_scopes.resolve(self.conn, scope)
        with self.conn.atomic():
            self.conn.execute('UPDATE playlist_scopes SET canvas_id=NULL,canvas_revision=NULL,canvas_created_at=NULL,'
                              'canvas_recovery_filename=NULL,canvas_recovery_digest=NULL,canvas_recovery_bytes=NULL '
                              'WHERE id=?', (scope,))

    @snapshot_read
    def get_recipe_created_ats(self):
        return self.document_created_ats('recipes')

    @snapshot_read
    def document_created_ats(self, name):
        return playlist_recipe_state.created_ats(self.conn, playlist_recipe_state.PREFIXES[name])
