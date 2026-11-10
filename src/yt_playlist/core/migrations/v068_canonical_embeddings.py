"""Canonical, dimensioned embedding spaces; ambiguous originals remain evidence."""
import hashlib
import json
import math
import struct

from .runner import MigrationError


SOURCES = {
    'rec_vectors': ('recording', 'collaborative', 'identity_key'),
    'rec_content_vectors': ('recording', 'content_library', 'identity_key'),
    'rec_discovered_content_vectors': ('recording', 'content_discovery', 'identity_key'),
    'rec_artist_vectors': ('artist', 'collaborative', 'artist'),
    'rec_artist_content_vectors': ('artist', 'content', 'artist'),
}


def statements():
    for kind, spaces in (('recording', "'collaborative','content_library','content_discovery'"),
                         ('artist', "'collaborative','content'")):
        table = f'{kind}_embeddings'
        yield (f'CREATE TABLE {table} ({kind}_id INTEGER NOT NULL REFERENCES {kind}s(id),'
               f'space TEXT NOT NULL CHECK(space IN ({spaces})),model_version TEXT NOT NULL '
               'CHECK(length(model_version)>0),dimensions INTEGER NOT NULL '
               "CHECK(typeof(dimensions)='integer' AND dimensions>0),vector BLOB NOT NULL "
               "CHECK(typeof(vector)='blob' AND length(vector)=dimensions*4),built_at REAL "
               "CHECK(built_at IS NULL OR (typeof(built_at) IN ('real','integer') "
               'AND abs(built_at)<=1.7976931348623157e308)),'
               f'PRIMARY KEY({kind}_id,space,model_version))')
        yield f'CREATE INDEX ix_{kind}_embedding_space ON {table}(space,model_version,{kind}_id)'
        for operation in ('INSERT', 'UPDATE'):
            yield (f'CREATE TRIGGER {table}_basis_{operation.lower()} BEFORE {operation} ON {table} '
                   f'WHEN EXISTS(SELECT 1 FROM (SELECT model_version,dimensions FROM {table} '
                   'WHERE space=NEW.space LIMIT 1) e WHERE '
                   "e.model_version<>NEW.model_version OR e.dimensions<>NEW.dimensions) "
                   "BEGIN SELECT RAISE(ABORT,'Mixed bases in an embedding space'); END")
            yield (f'CREATE TRIGGER {table}_owner_{operation.lower()} BEFORE {operation} ON {table} '
                   f'WHEN EXISTS(SELECT 1 FROM {kind}_redirects WHERE retired_id=NEW.{kind}_id) '
                   "BEGIN SELECT RAISE(ABORT,'Embedding owner is retired'); END")
            yield (f'CREATE TRIGGER retire_{kind}_embeddings_{operation.lower()} AFTER {operation} '
                   f'ON {kind}_redirects BEGIN DELETE FROM {table} WHERE {kind}_id=NEW.retired_id; END')
    yield ("CREATE TABLE unresolved_legacy_embeddings (source TEXT NOT NULL CHECK(source IN "
           "('rec_vectors','rec_content_vectors','rec_discovered_content_vectors',"
           "'rec_artist_vectors','rec_artist_content_vectors')),raw_key TEXT NOT NULL,"
           'model_version TEXT NOT NULL,dimensions INTEGER NOT NULL,vector BLOB NOT NULL,built_at REAL '
           "CHECK(built_at IS NULL OR (typeof(built_at) IN ('real','integer') "
           'AND abs(built_at)<=1.7976931348623157e308)),'
           "reason TEXT NOT NULL CHECK(reason IN ('missing_owner','ambiguous_owner','retired_owner')),"
           "PRIMARY KEY(source,raw_key),CHECK(typeof(dimensions)='integer' AND dimensions>0),"
           "CHECK(typeof(vector)='blob' AND length(vector)=dimensions*4)) WITHOUT ROWID")


def _basis(conn, source, kind, space, settings):
    if space.startswith('content'):
        raw = settings.get('rec_content_model' if kind == 'recording' else 'rec_artist_content_model')
        basis = ['embedding-basis-1', kind, 'content', json.loads(raw)] if raw else None
    else:
        inputs = settings.get('rec_fit_inputs' if kind == 'recording' else 'rec_artist_fit_inputs')
        basis = (['embedding-basis-1', kind, space, inputs,
                  (settings.get('rec_embed_method') or 'svd') if kind == 'recording' else 'svd',
                  (settings.get('rec_dim') or '48') if kind == 'recording' else '48']
                 if inputs else None)
    if basis is not None:
        return hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    # A missing encoder is explicitly unknown, never declared compatible solely
    # because its vectors happen to have the same number of dimensions.
    digest = hashlib.sha256(source.encode())
    for key, vector in conn.execute(f'SELECT {SOURCES[source][2]},vec FROM {source} ORDER BY rowid'):
        digest.update(json.dumps(key).encode())
        digest.update(vector)
    return 'legacy-unversioned:' + digest.hexdigest()


def _owner(conn, kind, key):
    if kind == 'recording':
        matches = [row[0] for row in conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,))]
        redirects = 'recording_redirects'
    else:
        redirects = 'artist_redirects'
        if key.startswith('@artist:') and key[8:].isdigit():
            matches = [row[0] for row in conn.execute('SELECT id FROM artists WHERE id=?', (int(key[8:]),))]
        else:
            matches = [row[0] for row in conn.execute('SELECT DISTINCT artist_id FROM artist_aliases '
                                                    'WHERE alias=?', (key,))]
    if not matches:
        return None, 'missing_owner'
    if len(matches) != 1:
        return None, 'ambiguous_owner'
    if conn.execute(f'SELECT 1 FROM {redirects} WHERE retired_id=?', (matches[0],)).fetchone():
        return None, 'retired_owner'
    return matches[0], None


def apply(conn, context):
    settings = dict(conn.execute('SELECT key,value FROM settings'))
    versions = {}
    # Diagnose invalid payloads before replacing any owner. Never truncate a
    # vector, fabricate a dimension, or discard nonfinite numerical evidence.
    for source, (kind, space, column) in SOURCES.items():
        versions[source] = _basis(conn, source, kind, space, settings)
        size = None
        for key, vector in conn.execute(f'SELECT {column},vec FROM {source} ORDER BY rowid'):
            if not isinstance(vector, bytes) or not vector or len(vector) % 4 or any(
                    not math.isfinite(value) for value, in struct.iter_unpack('=f', vector)):
                raise MigrationError(f'Invalid float32 embedding in {source}: {key!r}')
            dim = len(vector) // 4
            if size is not None and size != dim:
                raise MigrationError(f'Mixed embedding dimensions in {source}: {key!r}')
            size = dim
        if space.startswith('content'):
            raw = settings.get('rec_content_model' if kind == 'recording' else 'rec_artist_content_model')
            model = json.loads(raw) if raw else None
            if model is not None and size is not None and size != model['ncat'] + len(model['cont']):
                raise MigrationError(f'Embedding dimensions do not match the encoder in {source}')
    for sql in statements():
        conn.execute(sql)
    for source, (kind, space, column) in SOURCES.items():
        owners = {}
        for key, in conn.execute(f'SELECT {column} FROM {source}'):
            subject, reason = _owner(conn, kind, key)
            if reason is None:
                owners[subject] = owners.get(subject, 0) + 1
        for key, vector in conn.execute(f'SELECT {column},vec FROM {source} ORDER BY rowid'):
            subject, reason = _owner(conn, kind, key)
            if reason is None and owners[subject] != 1:
                # Retain every conflicting original; incidental row order does
                # not establish which encoder output belongs to the owner.
                reason = 'ambiguous_owner'
            if reason:
                conn.execute('INSERT INTO unresolved_legacy_embeddings VALUES (?,?,?,?,?,NULL,?)',
                             (source, key, versions[source], len(vector) // 4, vector, reason))
            else:
                conn.execute(f'INSERT INTO {kind}_embeddings VALUES (?,?,?,?,?,NULL)',
                             (subject, space, versions[source], len(vector) // 4, vector))
    for source in SOURCES:
        conn.execute(f'DROP TABLE {source}')


def validate(conn):
    from .current import _tokens
    from ..schema_inspection import objects
    definitions = {name: sql for name, (_kind, sql) in objects(conn).items()}
    for sql in statements():
        words = sql.split()
        name = words[3] if words[1] == 'UNIQUE' else words[2]
        actual = definitions.get(name)
        if actual is None or _tokens(actual) != _tokens(sql):
            raise MigrationError(f'Incompatible canonical embedding structure: {name}')
    for source in SOURCES:
        if source in definitions:
            raise MigrationError(f'Superseded embedding store remains: {source}')
