"""Temporary album controls retain canonical genre identities across label edits."""
import json

from . import genre_identity

KEYS = frozenset({'album_menu_tuning'})


def available(conn):
    from .preparation import control_publication
    return (conn.execute("SELECT 1 FROM sqlite_schema WHERE name='album_menu_controls'").fetchone() is not None
            or control_publication.installed(conn, 'album-controls'))


def get(conn,key,default=None):
    from .preparation import control_publication
    revision = control_publication.selected(conn, 'album-controls')
    if revision is not None:
        return control_publication.album_read(conn, revision, default)
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='album_menu_controls'").fetchone():
        row = conn.execute('SELECT value FROM main.settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default
    rows = list(conn.execute('SELECT p.expires_at,p.novelty,c.position,COALESCE(g.key,c.unresolved_value) '
        'FROM album_menu_controls p LEFT JOIN album_menu_control_genres c ON c.singleton=p.singleton '
        'LEFT JOIN genre_catalog g ON g.key=c.genre_key WHERE p.singleton=1 ORDER BY c.position'))
    if not rows:
        return default
    genres = [row[3] for row in rows if row[2] is not None]
    return json.dumps({'until':rows[0][0],'values':{'novelty':rows[0][1]},'genres':genres})


def put(conn,values):
    from .preparation import control_publication
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='album_menu_controls'").fetchone():
        return control_publication.retained_write(conn, values)
    for key,raw in values.items():
        conn.execute('DELETE FROM album_menu_controls WHERE singleton=1')
        if not raw:
            continue
        value = json.loads(raw)
        if not isinstance(value,dict) or set(value)!={'until','values','genres'}:
            raise ValueError('Unsupported temporary album controls')
        controls = value['values']
        if isinstance(controls,dict) and set(controls)=={'discovery','stretch','variety','recency'}:
            controls = {'novelty':controls['discovery']}
        genres = value['genres']
        if (not isinstance(controls,dict) or set(controls)!={'novelty'}
                or not isinstance(genres,list) or len(genres)>100
                or any(not isinstance(item,str) or not 0<len(item)<=100 for item in genres)):
            raise ValueError('Unsupported temporary album control selections')
        conn.execute('INSERT INTO album_menu_controls VALUES (1,?,?)',(value['until'],controls['novelty']))
        for position,label in enumerate(genres):
            resolved,_ = genre_identity.resolve(conn,label)
            conn.execute('INSERT INTO album_menu_control_genres VALUES (1,?,?,?)',
                         (position,resolved,None if resolved else label))
