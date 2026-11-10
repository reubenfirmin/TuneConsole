"""Ordered recipe selections reference identities; unresolved input stays explicit."""
from yt_playlist.core import artists, genre_identity


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recipe_artists'").fetchone() is not None


def replace(conn, recipe_id, artist_labels, include, exclude, *, legacy=False):
    for values in (artist_labels,include,exclude):
        if not isinstance(values,list) or any(not isinstance(v,str) or not v.strip() for v in values):
            raise ValueError('Recipe selections require lists of nonblank labels')
    for table in ('recipe_artists','recipe_genres','recipe_unresolved_artists','recipe_unresolved_genres'):
        conn.execute(f'DELETE FROM {table} WHERE recipe_id=?',(recipe_id,))
    seen = set()
    for position, label in enumerate(artist_labels):
        artist_id = artists.lookup(conn,label,exact=True)
        if artist_id is None:
            conn.execute('INSERT INTO recipe_unresolved_artists(recipe_id,position,raw_label,reason) VALUES (?,?,?,?)',
                         (recipe_id,position,label,'unknown_or_ambiguous_artist'))
        elif artist_id in seen:
            raise ValueError('A recipe cannot select the same artist twice')
        else:
            conn.execute('INSERT INTO recipe_artists VALUES (?,?,?)',(recipe_id,artist_id,position))
            seen.add(artist_id)
    for role, values in (('include',include),('exclude',exclude)):
        seen = set()
        for position,label in enumerate(values):
            key, reason = genre_identity.resolve(conn,label)
            if key is None and not legacy:
                key = genre_identity.add_custom(conn,label)
            if key is None:
                conn.execute('INSERT INTO recipe_unresolved_genres VALUES (?,?,?,?,?)',
                             (recipe_id,role,position,label,reason))
            elif key in seen:
                raise ValueError('A recipe cannot select the same genre twice in one role')
            else:
                conn.execute('INSERT INTO recipe_genres VALUES (?,?,?,?)',(recipe_id,key,role,position))
                seen.add(key)


def read(conn, recipe_id):
    artist_rows = conn.execute('SELECT m.position,a.name FROM recipe_artists m JOIN artists a ON a.id=m.artist_id '
                              'WHERE m.recipe_id=? UNION ALL SELECT position,raw_label FROM recipe_unresolved_artists '
                              'WHERE recipe_id=? ORDER BY position',(recipe_id,recipe_id))
    result = {'artists':[row[1] for row in artist_rows],'genres':[],'blacklist_genres':[]}
    for role,position,label in conn.execute('SELECT m.role,m.position,c.name FROM recipe_genres m '
            'JOIN genre_catalog c ON c.key=m.genre_key WHERE m.recipe_id=? UNION ALL '
            'SELECT role,position,raw_label FROM recipe_unresolved_genres WHERE recipe_id=? ORDER BY role,position',
            (recipe_id,recipe_id)):
        result['genres' if role=='include' else 'blacklist_genres'].append(label)
    return result
