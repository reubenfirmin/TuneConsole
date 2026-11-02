"""Saved playlist documents with relational current facets and frozen evidence."""
import uuid

from . import collection_recovery, taste_axes

FIELDS = ('media','model','journey','opportunity_id','option_id')


def available(conn):
    return any(row[1]=='playlist_scope_id' for row in conn.execute('PRAGMA table_info(rec_recipes)'))


def save(conn,scope,value,created_at):
    if not isinstance(value,dict):
        raise ValueError('A saved playlist recipe requires an object')
    conn.execute('UPDATE rec_recipes SET revision=revision WHERE 0')
    row = conn.execute('SELECT id FROM rec_recipes WHERE playlist_scope_id=?',(scope,)).fetchone()
    identity = row[0] if row else conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM rec_recipes').fetchone()[0]
    facets = []
    selections = {} if value.get('facets') is None else value['facets']
    if not isinstance(selections,dict):
        raise ValueError('Playlist recipe facets require an object')
    for kind,field in (('genre','genres'),('era','eras')):
        if field not in selections or selections[field] is None:
            continue
        if not isinstance(selections[field],list):
            raise ValueError('Playlist recipe facets require ordered lists')
        for position,label in enumerate(selections[field]):
            if not isinstance(label,str) or not label.strip():
                raise ValueError('Playlist recipe facets require nonempty labels')
            axis = taste_axes.identity(conn,('genre:' if kind=='genre' else 'era:')+label,create=True)
            facets.append((identity,kind,position,axis,label))
    reference = collection_recovery.publish(conn,'playlist-recipe',identity,value)
    conn.execute('INSERT INTO rec_recipes(id,playlist_scope_id,revision,created_at,media,model,journey,opportunity_id,option_id,'
                 'recovery_filename,recovery_digest,recovery_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
                 'ON CONFLICT(playlist_scope_id) DO UPDATE SET revision=excluded.revision,created_at=excluded.created_at,'
                 'media=excluded.media,model=excluded.model,journey=excluded.journey,opportunity_id=excluded.opportunity_id,'
                 'option_id=excluded.option_id,recovery_filename=excluded.recovery_filename,'
                 'recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes',
                 (identity,scope,uuid.uuid4().hex,created_at,*(value.get(field) for field in FIELDS),*reference))
    conn.execute('DELETE FROM playlist_recipe_facets WHERE recipe_id=?',(identity,))
    conn.executemany('INSERT INTO playlist_recipe_facets VALUES (?,?,?,?,?)',facets)
    from . import append_work
    if append_work.available(conn):
        append_work.save_tv(conn,identity,scope,value)


@collection_recovery.pinned_read
def load(conn,scope):
    row = conn.execute('SELECT * FROM rec_recipes WHERE playlist_scope_id=?',(scope,)).fetchone()
    if row is None:
        return None
    value = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                     kind='playlist-recipe',owner=row['id'])
    # Presence/null and unqueried attributes come from the captured document;
    # execution fields and ordered facets come from the current relational owner.
    value.update({field:row[field] for field in FIELDS if field in value})
    selections = {} if value.get('facets') is None else value['facets']
    for kind,field in (('genre','genres'),('era','eras')):
        if field not in selections or selections[field] is None:
            continue
        selections[field] = [item[0] for item in conn.execute(
            "SELECT CASE a.kind WHEN 'genre' THEN a.genre_key WHEN 'family' THEN a.family_key "
            "WHEN 'unresolved_genre' THEN a.value ELSE f.captured_value END "
            'FROM playlist_recipe_facets f JOIN taste_axes a ON a.id=f.axis_id '
            'WHERE f.recipe_id=? AND f.kind=? ORDER BY f.position',(row['id'],kind))]
    from . import append_work
    if append_work.available(conn):
        append_work.load_tv(conn,row['id'],value)
    else:
        from .append_reads import apply_tv
        apply_tv(conn,row['id'],value)
    return value


def save_canvas(conn,scope,state,created_at):
    if state is not None and not isinstance(state,str):
        raise ValueError('A saved canvas requires its serialized text')
    conn.execute('UPDATE cluster_canvas SET revision=revision WHERE 0')
    row = conn.execute('SELECT id FROM cluster_canvas WHERE playlist_scope_id=?',(scope,)).fetchone()
    identity = row[0] if row else conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM cluster_canvas').fetchone()[0]
    reference = collection_recovery.publish(conn,'playlist-canvas',identity,state)
    conn.execute('INSERT INTO cluster_canvas VALUES (?,?,?,?,?,?,?) '
                 'ON CONFLICT(playlist_scope_id) DO UPDATE SET revision=excluded.revision,created_at=excluded.created_at,'
                 'recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,'
                 'recovery_bytes=excluded.recovery_bytes',
                 (identity,scope,uuid.uuid4().hex,created_at,*reference))


@collection_recovery.pinned_read
def load_canvas(conn,scope):
    row = conn.execute('SELECT * FROM cluster_canvas WHERE playlist_scope_id=?',(scope,)).fetchone()
    return collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                    kind='playlist-canvas',owner=row['id']) if row else None
