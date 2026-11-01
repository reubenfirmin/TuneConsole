"""Resolve existing genre identities without guessing from spelling or family.

Display names, stable keys and registered aliases are independent evidence. A
collision between them is ambiguous, even when one happens to be an exact key.
Only explicit user additions create identities; provider labels never do.
"""
from yt_playlist.util.genre_catalog import normalize


def seed_catalog(conn):
    """Make verified identities available before preferences or metadata arrive."""
    from yt_playlist.util import genre_catalog
    records, labels = genre_catalog.catalog()
    conn.executemany('INSERT INTO genre_family(key,name) VALUES (?,?) ON CONFLICT DO NOTHING',
                     list(genre_catalog.family_labels().items()))
    conn.executemany('INSERT INTO genre_catalog(key,name,family_key,source,family_source,description) '
                     'VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                     [(r['key'],r['name'],r['family'],r['source'],r['family_source'],r.get('description',''))
                      for r in records.values()])
    conn.executemany('INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                     [(key,label,genre,source) for key,(genre,label,source) in labels.items()])


def resolve(conn, label):
    if label is None or label == '':
        return None, None
    if not isinstance(label, str):
        raise ValueError('A genre label must be text or null')
    matches = {row[0] for row in conn.execute(
        'SELECT key FROM genre_catalog WHERE key=? OR name=? COLLATE NOCASE '
        'UNION SELECT genre_key FROM genre_alias WHERE label_key=? OR label=? COLLATE NOCASE',
        (label, label, normalize(label), label))}
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_redirects'").fetchone():
        matches = {current_key(conn, key) for key in matches}
    if len(matches) == 1:
        return matches.pop(), None
    return None, 'ambiguous_genre' if matches else 'unknown_genre'


def current_key(conn, key):
    if not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_redirects'").fetchone():
        return key
    row = conn.execute('SELECT genre_key FROM genre_redirects WHERE retired_key=?', (key,)).fetchone()
    return row[0] if row else key


def family(conn, label):
    """Database family membership follows current canonical identity edits."""
    key, reason = resolve(conn, label)
    if key is not None:
        row = conn.execute('SELECT family_key FROM genre_catalog WHERE key=?', (key,)).fetchone()
        return row[0] or 'other:' + key
    if reason == 'ambiguous_genre':
        return 'other:' + normalize(label)
    from yt_playlist.util import genre_family_data
    return genre_family_data.family(label)


def add_custom(conn, label):
    """Admit a deliberate custom preference, preserving an existing identity."""
    if not isinstance(label, str) or not label.strip() or len(label.encode()) > 1024:
        raise ValueError('A custom genre requires 1..1024 bytes of nonblank text')
    key, reason = resolve(conn, label)
    if reason == 'ambiguous_genre':
        raise ValueError('Ambiguous genre label: ' + label)
    if key is not None:
        return key
    key = normalize(label)
    if not key:
        raise ValueError('A custom genre requires a nonempty identity')
    if conn.execute('SELECT 1 FROM genre_catalog WHERE key=?', (key,)).fetchone():
        raise ValueError('Custom genre key collides with an existing identity: ' + label)
    conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')", (key, label))
    conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,'custom')",
                 (key, label, key))
    return key
