"""SQL owners and detached file references for saved playlist documents."""
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.core import append_reads, append_work, playlist_recipe_state, playlist_scopes, taste_axes
from yt_playlist.core.preparation.collection_document_publication import OWNERS
from yt_playlist.core.preparation.collection_documents import table as prepared_table


class PlaylistRecipesRepo(Repo):
    @synchronized
    def document_owner(self, name, scope):
        with self.conn.atomic():
            scope = playlist_scopes.ensure(self.conn, scope)
        native = playlist_recipe_state.available(self.conn)
        table = 'rec_recipes' if name == 'recipes' else 'cluster_canvas'
        row = self.conn.execute(f'SELECT id FROM {table} WHERE playlist_scope_id=?', (scope,)).fetchone() if native else None
        identity = None
        if native:
            identity = row[0] if row else self.conn.execute(
                f'SELECT COALESCE(MAX(id),0)+1 FROM {table}').fetchone()[0]
        return {'scope': scope, 'native': native, 'id': identity}

    @synchronized
    def publish_legacy(self, name, scope, raw, now):
        table, column = ('rec_recipes', 'recipe') if name == 'recipes' else ('cluster_canvas', 'state')
        with self.conn.atomic():
            self.conn.execute(f'INSERT INTO {table}(playlist_ytm,{column},created_at) VALUES (?,?,?) '
                f'ON CONFLICT(playlist_ytm) DO UPDATE SET {column}=excluded.{column},created_at=excluded.created_at',
                (scope, raw, now))

    @synchronized
    def publish_recipe(self, owner, revision, value, facets, reference, append, now):
        identity = owner['id']
        with self.conn.atomic():
            rows = [(identity, kind, position, taste_axes.identity(self.conn, axis, create=True), label)
                    for kind, position, axis, label in facets]
            self.conn.execute('INSERT INTO rec_recipes(id,playlist_scope_id,revision,created_at,media,model,journey,'
                'opportunity_id,option_id,recovery_filename,recovery_digest,recovery_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(playlist_scope_id) DO UPDATE SET revision=excluded.revision,created_at=excluded.created_at,'
                'media=excluded.media,model=excluded.model,journey=excluded.journey,opportunity_id=excluded.opportunity_id,'
                'option_id=excluded.option_id,recovery_filename=excluded.recovery_filename,'
                'recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes',
                (identity, owner['scope'], revision, now, *(value.get(field) for field in playlist_recipe_state.FIELDS), *reference))
            self.conn.execute('DELETE FROM playlist_recipe_facets WHERE recipe_id=?', (identity,))
            self.conn.executemany('INSERT INTO playlist_recipe_facets VALUES (?,?,?,?,?)', rows)
            if append is not None:
                append_work.install_tv(self.conn, identity, owner['scope'], append)

    @synchronized
    def publish_canvas(self, owner, revision, reference, now):
        with self.conn.atomic():
            self.conn.execute('INSERT INTO cluster_canvas VALUES (?,?,?,?,?,?,?) '
                'ON CONFLICT(playlist_scope_id) DO UPDATE SET revision=excluded.revision,created_at=excluded.created_at,'
                'recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes',
                (owner['id'], owner['scope'], revision, now, *reference))

    @snapshot_read
    def document_facts(self, name, scope):
        scope = playlist_scopes.resolve(self.conn, scope)
        table, field = ('rec_recipes', 'recipe') if name == 'recipes' else ('cluster_canvas', 'state')
        native = playlist_recipe_state.available(self.conn)
        if native:
            row = self.conn.execute(f'SELECT * FROM {table} WHERE playlist_scope_id=?', (scope,)).fetchone()
            if row is None:
                return None
            facts = {'scope': scope, 'native': True, 'header': dict(row)}
            if name == 'recipes':
                facts['facets'] = [tuple(item) for item in self.conn.execute(
                    "SELECT f.kind,CASE a.kind WHEN 'genre' THEN a.genre_key WHEN 'family' THEN a.family_key "
                    "WHEN 'unresolved_genre' THEN a.value ELSE f.captured_value END "
                    'FROM playlist_recipe_facets f JOIN taste_axes a ON a.id=f.axis_id '
                    'WHERE f.recipe_id=? ORDER BY f.kind,f.position', (row['id'],))]
            return facts
        owner = OWNERS[name]
        stage = owner.selected(self.conn)
        if stage is not None:
            header = owner.capture(self.conn, stage, scope)
            if header is None:
                return None
            parts = [dict(row) for row in self.conn.execute('SELECT * FROM '+prepared_table(name, 'parts')+
                ' WHERE source=? AND preparation_revision=? ORDER BY position LIMIT 513', (scope, header['preparation_revision']))]
            return {'scope': scope, 'native': False, 'prepared': True, 'header': dict(header), 'parts': parts}
        row = self.conn.execute(f'SELECT {field} FROM {table} WHERE playlist_ytm=?', (scope,)).fetchone()
        return {'scope': scope, 'native': False, 'prepared': False, 'raw': row[0]} if row else None

    @lookup_read
    def append_available(self):
        return append_work.available(self.conn)

    @lookup_read
    def append_owner(self, scope, stream, request):
        row = self.conn.execute('SELECT * FROM tv_append_intents WHERE playlist_scope_id=? AND stream_token=? AND request_id=?',
                                (scope, stream, request)).fetchone()
        identity = row['id'] if row else self.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM tv_append_intents').fetchone()[0]
        return {'id': identity, 'prior': dict(row) if row else None}

    @snapshot_read
    def append_facts(self, scope, recipe, value):
        if append_work.available(self.conn):
            checkpoint = value.get('tv_refill')
            if recipe is None or not isinstance(checkpoint, dict):
                return None
            row = self.conn.execute('SELECT * FROM tv_append_intents WHERE recipe_id=? AND stream_token=? AND request_id=?',
                (recipe, value.get('tv_stream'), checkpoint.get('request_id'))).fetchone()
            roles = {role: [item[0] for item in self.conn.execute('SELECT video_id_snapshot FROM tv_append_items '
                'WHERE intent_id=? AND role=? ORDER BY position', (row['id'], role))]
                for role in ('before', 'additions', 'items')} if row else {}
            return {'native': True, 'header': dict(row) if row else None, 'roles': roles}
        name = 'tv_native' if recipe is not None else 'tv_legacy'
        row = append_reads.selected_operation(self.conn, name, recipe if recipe is not None else scope)
        return {'native': False, 'header': dict(row), 'roles': {
            role: append_reads.sequence(self.conn, name, row, role) for role in ('before', 'additions', 'items')}} if row else None

    @snapshot_read
    def recent_video_playlist_refs(self, limit=20):
        if playlist_recipe_state.available(self.conn):
            return [row[0] for row in self.conn.execute("SELECT playlist_scope_id FROM rec_recipes WHERE media='video' "
                'ORDER BY created_at DESC,playlist_scope_id LIMIT ?', (limit,))]
        owner = OWNERS['recipes']
        stage = owner.selected(self.conn)
        if stage is not None:
            return [row[0] for row in self.conn.execute('SELECT m.source FROM '+owner.table('map')+' m JOIN '+owner.header+
                " g ON g.source=m.source AND g.preparation_revision=m.generation WHERE m.stage=? AND g.media='video' "
                'ORDER BY g.created_at DESC,m.source LIMIT ?', (stage, limit))]
        return [row[0] for row in self.conn.execute('SELECT playlist_ytm FROM rec_recipes WHERE CASE WHEN json_valid(recipe) '
            "THEN json_extract(recipe,'$.media') END='video' ORDER BY created_at DESC,playlist_ytm LIMIT ?", (limit,))]

    @synchronized
    def delete_cluster_canvas(self, scope):
        scope = playlist_scopes.resolve(self.conn, scope)
        column = 'playlist_scope_id' if playlist_recipe_state.available(self.conn) else 'playlist_ytm'
        with self.conn.atomic():
            self.conn.execute(f'DELETE FROM cluster_canvas WHERE {column}=?', (scope,))

    @snapshot_read
    def get_recipe_created_ats(self):
        native = playlist_recipe_state.available(self.conn)
        if not native:
            owner = OWNERS['recipes']
            stage = owner.selected(self.conn)
            if stage is not None:
                return {row[0]: row[1] for row in self.conn.execute('SELECT m.source,g.created_at FROM '+owner.table('map')+
                    ' m JOIN '+owner.header+' g ON g.source=m.source AND g.preparation_revision=m.generation WHERE m.stage=?', (stage,))}
        column = 'playlist_scope_id' if native else 'playlist_ytm'
        return dict(self.conn.execute(f'SELECT {column},created_at FROM rec_recipes'))
