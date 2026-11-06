"""Relational taste axes; encoded tokens exist only at the legacy API boundary."""
from yt_playlist.core import genre_identity, taste_reads
from yt_playlist.util import genre_catalog

TABLES = ('rec_weights', 'rec_lean', 'home_hidden_facet', 'rec_theme',
          'play_gradient_checkpoints', 'recommendation_gradient_events')


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='taste_axes'").fetchone() is not None


def identity(conn, token, *, create=False):
    if not available(conn):
        return token
    if not isinstance(token, str) or not token:
        raise ValueError('A taste axis requires a nonempty token')
    kind, genre, family, value, reason = 'other', None, None, token, None
    if token.startswith(('genre:', 'family:')):
        label = token.split(':', 1)[1]
        # Legacy genre:<family> bars mean a broad family, even when a specific
        # genre has the same name. Preserve that established interpretation.
        if conn.execute('SELECT 1 FROM genre_family WHERE key=?',(label,)).fetchone():
            kind, family, value = 'family', label, None
        else:
            genre, reason = genre_identity.resolve(conn, label)
            if genre is None and reason == 'unknown_genre' and create:
                records, labels = genre_catalog.catalog()
                verified = labels.get(genre_catalog.normalize(label))
                if verified:
                    record = records[verified[0]]
                    family_key = record['family']
                    if family_key:
                        conn.execute('INSERT INTO genre_family(key,name) VALUES (?,?) ON CONFLICT DO NOTHING',
                                     (family_key, family_key.title()))
                    conn.execute('INSERT INTO genre_catalog(key,name,family_key,source) VALUES (?,?,?,?) '
                                 'ON CONFLICT DO NOTHING',
                                 (verified[0], record['name'], family_key, 'catalog'))
                    genre, reason = genre_identity.resolve(conn, verified[0])
            if genre is not None:
                kind, value = 'genre', None
            else:
                kind, value = 'unresolved_genre', label
    fields = (kind, genre, family, value)
    row = conn.execute('SELECT id FROM taste_axes WHERE kind=? AND genre_key IS ? '
                       'AND family_key IS ? AND value IS ?', fields).fetchone()
    if row:
        return row[0]
    if not create:
        return None
    return conn.execute('INSERT INTO taste_axes(kind,genre_key,family_key,value,resolution_reason) '
                        'VALUES (?,?,?,?,?)', (*fields, reason)).lastrowid


def projection(conn, table, columns='*', *, token_name='axis'):
    if not available(conn):
        prepared = taste_reads.projection(conn,table,columns,token_name=token_name)
        if prepared is not None:
            return prepared
        if compatible(conn):
            return taste_reads.live_projection(table,columns,token_name=token_name)
        return f'SELECT {columns} FROM {table} t'
    # IDs stay stable when display names or aliases change. Existing callers
    # consume stable genre keys and family keys through their token boundary.
    token = ("CASE a.kind WHEN 'genre' THEN 'genre:'||a.genre_key "
             "WHEN 'family' THEN 'genre:'||a.family_key "
             "WHEN 'unresolved_genre' THEN 'genre:'||a.value ELSE a.value END")
    names = [c.strip() for c in columns.split(',')]
    selected = ','.join(f'{token} AS {token_name}' if c == token_name else 't.' + c for c in names)
    return f'SELECT {selected} FROM {table} t JOIN taste_axes a ON a.id=t.axis_id'


def column(conn, legacy='axis'):
    return 'axis_id' if available(conn) else legacy


def table(conn, legacy):
    return ({'rec_play_grad': 'play_gradient_checkpoints',
             'rec_grad_log': 'recommendation_gradient_events'}.get(legacy, legacy)
            if available(conn) else legacy)


def compatible(conn):
    """Keep canonical API writes compatible after a published selection is withdrawn."""
    return (not available(conn) and conn.execute(
        "SELECT 1 FROM main.sqlite_schema WHERE name='published_taste_head'").fetchone() is not None
        and conn.execute('SELECT 1 FROM main.published_taste_head WHERE id=1').fetchone() is not None)


def matches(conn, owner, token, *, create=False):
    """Match current physical aliases using the same identity rules as preparation.

    The SQL predicate also covers newly added source aliases and catalog edits;
    an old publication map cannot establish their current identity. Only matched
    keys cross the Python boundary. Graduation events are appended separately.
    """
    legacy = 'facet' if owner=='rec_theme' else 'axis'
    if available(conn):
        return 'axis_id=?', (identity(conn,token,create=create),)
    if not compatible(conn):
        return legacy+'=?', (token,)
    _, (kind,genre,family,value,_) = taste_reads.axis(conn,token)
    if kind=='other':
        return legacy+'=?', (value,)
    prefixed,label,candidates = taste_reads.legacy_parts(legacy)
    if kind=='family':
        return prefixed+' AND ('+label+')=?', (family,)
    if kind=='unresolved_genre':
        return prefixed+' AND ('+label+')=?', (value,)
    return (prefixed+f' AND NOT EXISTS(SELECT 1 FROM genre_family WHERE key=({label})) '
        'AND (SELECT count(*)=1 AND min(identity_key)=? FROM ('+candidates+'))', (genre,))


def reference(conn, owner, token, *, create=False):
    """Resolve one mutable physical key without introducing a competing alias."""
    if available(conn):
        return identity(conn,token,create=create)
    if not compatible(conn):
        return token
    column = 'facet' if owner=='rec_theme' else 'axis'
    predicate,parameters = matches(conn,owner,token)
    rows = conn.execute('SELECT '+column+' FROM main.'+owner+' WHERE '+predicate+' ORDER BY '+column+' LIMIT 2',parameters).fetchall()
    if len(rows)>1 and owner in ('rec_weights','rec_lean'):
        raise ValueError('Multiple configured taste values resolve to one identity; reconcile the source explicitly')
    return rows[0][0] if rows else token


def lookup(conn, owner, token, columns):
    """Read a selected fold, or the current compatible physical contribution(s)."""
    projected = taste_reads.projection(conn,owner,'*',token_name='facet' if owner=='rec_theme' else 'axis')
    if projected is not None:
        _, (kind,genre,family,value,_) = taste_reads.axis(conn,token)
        canonical = ('genre:'+genre if kind=='genre' else 'genre:'+family if kind=='family'
                     else 'genre:'+value if kind=='unresolved_genre' else value)
        return conn.execute('SELECT '+columns+' FROM ('+projected+') WHERE '+
            ('facet' if owner=='rec_theme' else 'axis')+'=?',(canonical,)).fetchone()
    predicate,parameters = matches(conn,owner,token)
    return conn.execute('SELECT '+columns+' FROM main.'+table(conn,owner)+' WHERE '+predicate,parameters).fetchone()
