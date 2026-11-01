"""Prepare exact legacy taste contributions without replacing active values."""
import hashlib
import json

from .. import genre_identity
from .rows import Job, main as run


KEYS = {'rec_weights': ('axis',), 'rec_lean': ('axis',), 'home_hidden_facet': ('axis',),
        'rec_theme': ('facet',), 'rec_play_grad': ('axis',), 'rec_grad_log': ('id',)}
VALUES = ('weight', 'value', 'score', 'updated_at', 'last_graduated_day', 'source', 'factor', 'new_weight', 'created_at')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def axis(conn, token):
    if not isinstance(token, str) or not token or len(token.encode()) > 4096:
        raise ValueError('A taste axis requires 1..4096 bytes of text')
    kind, genre, family, value, reason = 'other', None, None, token, None
    if token.startswith(('genre:', 'family:')):
        label = token.split(':', 1)[1]
        # Existing family bars have priority over a genre of the same name.
        if conn.execute('SELECT 1 FROM genre_family WHERE key=?', (label,)).fetchone():
            kind, family, value = 'family', label, None
        else:
            genre, reason = genre_identity.resolve(conn, label)
            if genre is not None:
                kind, value = 'genre', None
            else:
                kind, value = 'unresolved_genre', label
                reason = reason or 'unknown_genre'
    fields = kind, genre, family, value, reason
    identity = hashlib.sha256(encoded(fields).encode()).hexdigest()
    return identity, fields


def conflicts(conn, owner, identity):
    if owner not in ('rec_weights', 'rec_lean') or identity is None:
        return
    keys = [row[0] for row in conn.execute('SELECT source_key FROM prepared_taste_rows '
        'WHERE owner=? AND axis_key=? ORDER BY source_key LIMIT 2', (owner, identity))]
    conn.execute('DELETE FROM prepared_taste_conflicts WHERE owner=? AND axis_key=?', (owner, identity))
    if len(keys) == 2:
        conn.execute('INSERT INTO prepared_taste_conflicts VALUES (?,?,?,?)', (owner, identity, *keys))


def describe(conn, owner, key, row):
    token = row['facet' if owner == 'rec_theme' else 'axis']
    identity, fields = axis(conn, token)
    snapshot = encoded(row)
    if len(snapshot.encode())>64*1024:
        raise ValueError('Taste source exceeds its preparation byte budget')
    # Include the observed resolution as well as source bytes. Catalog edits
    # must not rewrite the meaning retained with an earlier preparation batch.
    digest = hashlib.sha256(encoded([snapshot, fields]).encode()).hexdigest()
    return (owner, key, token, identity, digest, *(row.get(name) for name in VALUES)), fields, snapshot


def copy(conn, owner, key, row):
    values, fields, snapshot = describe(conn, owner, key, row)
    _, _, token, identity, digest, *_ = values
    prior = conn.execute('SELECT axis_key FROM prepared_taste_rows WHERE owner=? AND source_key=?',
                         (owner, key)).fetchone()
    conn.execute('INSERT INTO prepared_taste_axis_keys VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                 (identity, *fields))
    conn.execute('INSERT INTO prepared_taste_originals VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING',
                 (owner, key, digest, snapshot, identity))
    conn.execute('INSERT INTO prepared_taste_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(owner,source_key) DO UPDATE SET raw_axis=excluded.raw_axis,axis_key=excluded.axis_key,'
        'source_digest=excluded.source_digest,' + ','.join(name+'=excluded.'+name for name in VALUES),
        values)
    conflicts(conn, owner, identity)
    if prior is not None and prior[0] != identity:
        conflicts(conn, owner, prior[0])


def delete(conn, owner, key):
    prior = conn.execute('SELECT axis_key FROM prepared_taste_rows WHERE owner=? AND source_key=?',
                         (owner, key)).fetchone()
    conn.execute('DELETE FROM prepared_taste_rows WHERE owner=? AND source_key=?', (owner, key))
    if prior is not None:
        conflicts(conn, owner, prior[0])


JOB = Job('taste_axes', 79, 87, KEYS, (
    ('prepared_taste_axis_keys', '''CREATE TABLE prepared_taste_axis_keys (
        axis_key TEXT PRIMARY KEY CHECK(length(axis_key)=64),
        kind TEXT NOT NULL CHECK(kind IN ('genre','family','other','unresolved_genre')),
        genre_key TEXT, family_key TEXT, value TEXT,
        resolution_reason TEXT CHECK(resolution_reason IN ('unknown_genre','ambiguous_genre')),
        CHECK((kind='genre' AND genre_key IS NOT NULL AND family_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
           OR (kind='family' AND family_key IS NOT NULL AND genre_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
           OR (kind='other' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL AND resolution_reason IS NULL)
           OR (kind='unresolved_genre' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL AND resolution_reason IS NOT NULL))) WITHOUT ROWID'''),
    ('prepared_taste_originals', '''CREATE TABLE prepared_taste_originals (
        owner TEXT NOT NULL, source_key TEXT NOT NULL CHECK(json_valid(source_key)),
        digest TEXT NOT NULL CHECK(length(digest)=64), source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)),
        axis_key TEXT NOT NULL REFERENCES prepared_taste_axis_keys(axis_key),
        PRIMARY KEY(owner,source_key,digest)) WITHOUT ROWID'''),
    ('prepared_taste_rows', '''CREATE TABLE prepared_taste_rows (
        owner TEXT NOT NULL CHECK(owner IN ('rec_weights','rec_lean','home_hidden_facet','rec_theme','rec_play_grad','rec_grad_log')),
        source_key TEXT NOT NULL, raw_axis TEXT NOT NULL, axis_key TEXT NOT NULL REFERENCES prepared_taste_axis_keys(axis_key),
        source_digest TEXT NOT NULL, weight REAL, value REAL, score REAL, updated_at REAL,
        last_graduated_day TEXT, source TEXT, factor REAL, new_weight REAL, created_at REAL,
        PRIMARY KEY(owner,source_key),
        FOREIGN KEY(owner,source_key,source_digest) REFERENCES prepared_taste_originals(owner,source_key,digest)) WITHOUT ROWID'''),
    ('ix_prepared_taste_rows_axis', 'CREATE INDEX ix_prepared_taste_rows_axis ON prepared_taste_rows(owner,axis_key,source_key)'),
    ('prepared_taste_conflicts', '''CREATE TABLE prepared_taste_conflicts (
        owner TEXT NOT NULL CHECK(owner IN ('rec_weights','rec_lean')),
        axis_key TEXT NOT NULL REFERENCES prepared_taste_axis_keys(axis_key),
        first_source_key TEXT NOT NULL, second_source_key TEXT NOT NULL,
        PRIMARY KEY(owner,axis_key), CHECK(first_source_key<second_source_key)) WITHOUT ROWID'''),
), copy, delete, dependencies=('genre_catalog', 'genre_alias', 'genre_family'))


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
