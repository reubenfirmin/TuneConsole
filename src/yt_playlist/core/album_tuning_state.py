"""Temporary album controls retain canonical genre identities across label edits."""
import json

from . import genre_identity
from .recommendation_work import finite

KEYS = frozenset({'album_menu_tuning'})
TUNING = 'album_menu_tuning'
REFRESH = 'album_menu_refresh'


def get(conn, key, default=None):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    if row is None:
        return default
    value = json.loads(row[0])
    value['genres'] = [row[0] for row in conn.execute('''SELECT
        COALESCE(g.redirect_key,g.key,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]'))
        FROM json_each(?) c LEFT JOIN genre_catalog g ON g.key=json_extract(c.value,'$[0]') ORDER BY c.key''',
        (json.dumps(value['genres']),))]
    return json.dumps(value)


def put(conn,values):
    for raw in values.values():
        if not raw:
            conn.execute('DELETE FROM settings WHERE key=?', (TUNING,))
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
        if not 0 <= finite(controls['novelty']) <= 1:
            raise ValueError('Album novelty must be between zero and one')
        ordered = []
        for label in genres:
            resolved, _ = genre_identity.resolve(conn, label)
            ordered.append((resolved, None if resolved else label))
        stored = {'until': finite(value['until']), 'values': controls, 'genres': ordered}
        conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (TUNING, json.dumps(stored)))
