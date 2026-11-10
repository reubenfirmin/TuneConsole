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


def feature_axes(conn, labels):
    """Resolve a bounded set with the same collision and redirect rules as resolve."""
    return axes_from_matches(axis_matches(conn, labels))


def axis_matches(conn, labels):
    """Detach bounded current identity/family candidates without choosing a match."""
    labels = list(dict.fromkeys(label for label in labels if label))
    if any(not isinstance(label, str) for label in labels):
        raise ValueError('A genre label must be text or null')
    return axis_match_rows(conn, [(label, normalize(label)) for label in labels])


def axis_match_rows(conn, labels):
    """Read canonical identity/family candidates for supplied label/normalized pairs."""
    if not labels:
        return {}
    redirected = conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_redirects'").fetchone() is not None
    result = {}
    for offset in range(0, len(labels), 200):
        page = labels[offset:offset + 200]
        wanted = ','.join('(?,?)' for _ in page)
        current = 'COALESCE(d.genre_key,m.genre_key)' if redirected else 'm.genre_key'
        redirects = 'LEFT JOIN genre_redirects d ON d.retired_key=m.genre_key ' if redirected else ''
        # Filter unindexed display labels once before joining the requested
        # labels. Otherwise SQLite can scan the complete alias/name tables for
        # every label in a large first-selection batch.
        rows = conn.execute('WITH wanted(label,normalized) AS (VALUES ' + wanted + '),'
            'names AS MATERIALIZED (SELECT name,key FROM genre_catalog '
            'WHERE name COLLATE NOCASE IN (SELECT label FROM wanted)),'
            'aliases AS MATERIALIZED (SELECT label,genre_key FROM genre_alias '
            'WHERE label COLLATE NOCASE IN (SELECT label FROM wanted)),matches AS ('
            'SELECT w.label,c.key genre_key FROM wanted w JOIN genre_catalog c ON c.key=w.label '
            'UNION SELECT w.label,c.key FROM wanted w JOIN names c ON c.name=w.label COLLATE NOCASE '
            'UNION SELECT w.label,a.genre_key FROM wanted w JOIN genre_alias a ON a.label_key=w.normalized '
            'UNION SELECT w.label,a.genre_key FROM wanted w JOIN aliases a ON a.label=w.label COLLATE NOCASE) '
            'SELECT m.label,' + current + ',c.family_key FROM matches m ' + redirects +
            'JOIN genre_catalog c ON c.key=' + current,
            [value for pair in page for value in pair])
        matches = {label: {} for label, normalized in page}
        for label, key, family_key in rows:
            matches[label][key] = family_key
        result.update(matches)
    return result


def axes_from_matches(matches):
    from yt_playlist.util import genre_family_data
    result = {}
    for label, choices in matches.items():
        if len(choices) == 1:
            key, family_key = next(iter(choices.items()))
            result[label] = family_key or 'other:' + key, key
        else:
            result[label] = ('other:' + normalize(label) if choices else genre_family_data.family(label)), None
    return result


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
