"""Saved recipe and canvas fields on a playlist scope."""
import json
import uuid

from . import append_work, collection_recovery, playlist_recipe_policy, taste_axes

PREFIXES = {'recipes': 'recipe', 'canvases': 'canvas'}
REFERENCE = ('recovery_filename', 'recovery_digest', 'recovery_bytes')
HEADER = ('id', 'revision', 'created_at', *REFERENCE)
RECIPE_FIELDS = (*playlist_recipe_policy.FIELDS, 'facets')
COUNTERS = {prefix: 'playlist_' + prefix + '_id_high_water' for prefix in PREFIXES.values()}


def reserve_identity(conn, scope, prefix):
    identity = conn.execute(f'SELECT {prefix}_id FROM playlist_scopes WHERE id=?', (scope,)).fetchone()[0]
    if identity is None:
        identity = conn.execute('UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key=? '
                                'RETURNING CAST(value AS INTEGER)', (COUNTERS[prefix],)).fetchone()[0]
        conn.execute(f'UPDATE playlist_scopes SET {prefix}_id=? WHERE id=?', (identity, scope))
    return identity


def header(conn, scope, prefix):
    fields = HEADER + (RECIPE_FIELDS if prefix == 'recipe' else ())
    columns = ','.join(f'{prefix}_{field} AS {field}' for field in fields)
    return conn.execute(f'SELECT {columns} FROM playlist_scopes '
                        f'WHERE playlist_scopes.id=? AND {prefix}_revision IS NOT NULL', (scope,)).fetchone()


def facets(conn, value, selections):
    rows = [(kind, position, taste_axes.identity(conn, axis, create=True), label)
            for kind, position, axis, label in selections]
    return (*(value.get(field) for field in playlist_recipe_policy.FIELDS), json.dumps(rows))


def current_facets(conn, facets):
    return [tuple(item) for item in conn.execute("""
        SELECT f.value->>0,
               CASE a.kind WHEN 'genre' THEN coalesce(g.redirect_key,a.genre_key)
                    WHEN 'family' THEN a.family_key WHEN 'unresolved_genre' THEN a.value ELSE f.value->>3 END
        FROM json_each(?) f LEFT JOIN taste_axes a ON a.id=f.value->>2
        LEFT JOIN genre_catalog g ON g.key=a.genre_key
        ORDER BY f.key
    """, (facets,))]


def publish(conn, scope, prefix, identity, revision, created_at, reference, fields=()):
    columns = HEADER[1:] + (RECIPE_FIELDS if prefix == 'recipe' else ())
    assignments = ','.join(f'{prefix}_{field}=?' for field in columns)
    updated = conn.execute(f'UPDATE playlist_scopes SET {assignments} WHERE id=? AND {prefix}_id=?',
                           (revision, created_at, *reference, *fields, scope, identity))
    if not updated.rowcount:
        raise ValueError('Saved playlist identity changed during publication')


def created_ats(conn, prefix):
    return dict(conn.execute(f'SELECT id,{prefix}_created_at FROM playlist_scopes '
                             f'WHERE {prefix}_revision IS NOT NULL'))


def save(conn, scope, value, created_at):
    fields = facets(conn, value, playlist_recipe_policy.facets(value))
    identity = reserve_identity(conn, scope, 'recipe')
    recipe, append = append_work.prepare(value)
    reference = collection_recovery.publish(conn, 'playlist-recipe', identity, recipe)
    publish(conn, scope, 'recipe', identity, uuid.uuid4().hex, created_at, reference, fields)
    append_work.install(conn, scope, append)


@collection_recovery.pinned_read
def load(conn, scope):
    row = header(conn, scope, 'recipe')
    if row is None:
        return None
    value = collection_recovery.read(*(row[field] for field in REFERENCE), kind='playlist-recipe', owner=row['id'])
    playlist_recipe_policy.project(value, row, current_facets(conn, row['facets']))
    if 'tv_refill' in value:
        append_work.project(value, append_work.entries(conn, scope))
    return value


def save_canvas(conn, scope, state, created_at):
    if state is not None and not isinstance(state, str):
        raise ValueError('A saved canvas requires its serialized text')
    identity = reserve_identity(conn, scope, 'canvas')
    reference = collection_recovery.publish(conn, 'playlist-canvas', identity, state)
    publish(conn, scope, 'canvas', identity, uuid.uuid4().hex, created_at, reference)


@collection_recovery.pinned_read
def load_canvas(conn, scope):
    row = header(conn, scope, 'canvas')
    return (collection_recovery.read(*(row[field] for field in REFERENCE), kind='playlist-canvas', owner=row['id'])
            if row else None)
