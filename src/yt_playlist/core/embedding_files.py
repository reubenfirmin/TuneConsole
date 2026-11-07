"""Exact binary embedding chunks and typed canonical subject heads (#694)."""
from yt_playlist.core import file_paths
import hashlib
import struct
from pathlib import Path

from . import fao, file_collection

MAX_ROWS = 128
MAX_BYTES = 8 * 1024 * 1024
HEADER = struct.Struct('>16sII')
MAGIC = b'TC-embedding-1'.ljust(16, b'\0')


class EmbeddingFiles:
    def __init__(self, root):
        self.root = Path(root)

    def retained(self):
        return file_collection.retained_reader(self.root)

    def read_many(self, kind, rows):
        return _load_many(self.root, kind, rows)

    def write(self, kind, dimensions, rows):
        return _prepare(self.root, kind, dimensions, rows)


def definitions():
    for kind, spaces in (('recording', "'collaborative','content_library','content_discovery'"),
                         ('artist', "'collaborative','content'")):
        table = kind + '_embedding_files'
        yield table, (f'CREATE TABLE {table} ({kind}_id INTEGER NOT NULL REFERENCES {kind}s(id),'
            f'space TEXT NOT NULL CHECK(space IN ({spaces})),model_version TEXT NOT NULL CHECK(length(model_version)>0),'
            "dimensions INTEGER NOT NULL CHECK(typeof(dimensions)='integer' AND dimensions>0),publication BLOB NOT NULL CHECK(length(publication)=32),"
            'position INTEGER NOT NULL CHECK(position>=0 AND position<128),insertion_order INTEGER NOT NULL CHECK(insertion_order>0),'
            f"built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('real','integer') AND abs(built_at)<=1.7976931348623157e308)),PRIMARY KEY({kind}_id,space,model_version))")
        yield 'ix_' + table + '_space', f'CREATE INDEX ix_{table}_space ON {table}(space,model_version,{kind}_id)'
        for operation in ('insert', 'update'):
            name = kind+'_embedding_plain_'+operation
            yield name, (f'CREATE TRIGGER {name} AFTER {operation.upper()} ON {kind}_embeddings '
                f'BEGIN DELETE FROM {table} WHERE {kind}_id=NEW.{kind}_id AND space=NEW.space; END')
            for owner in (table, kind + '_embeddings'):
                name = owner + '_file_basis_' + operation
                other = kind + '_embeddings' if owner == table else table
                yield name, (f'CREATE TRIGGER {name} BEFORE {operation.upper()} ON {owner} '
                    f'WHEN EXISTS(SELECT 1 FROM {other} WHERE space=NEW.space '
                    'AND (model_version<>NEW.model_version OR dimensions<>NEW.dimensions)) '
                    "BEGIN SELECT RAISE(ABORT,'Mixed bases in an embedding space'); END")
            name = table + '_basis_' + operation
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation.upper()} ON {table} '
                f'WHEN EXISTS(SELECT 1 FROM {table} WHERE space=NEW.space '
                'AND (model_version<>NEW.model_version OR dimensions<>NEW.dimensions)) '
                "BEGIN SELECT RAISE(ABORT,'Mixed bases in an embedding space'); END")
            name = table + '_owner_' + operation
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation.upper()} ON {table} '
                f'WHEN EXISTS(SELECT 1 FROM {kind}_redirects WHERE retired_id=NEW.{kind}_id) '
                "BEGIN SELECT RAISE(ABORT,'Embedding owner is retired'); END")
            name = 'retire_' + table + '_' + operation
            yield name, (f'CREATE TRIGGER {name} AFTER {operation.upper()} ON {kind}_redirects '
                f'BEGIN DELETE FROM {table} WHERE {kind}_id=NEW.retired_id; END')


def available(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recording_embedding_files'").fetchone() is not None


def install(conn, context=None):
    for name, sql in definitions():
        conn.execute(sql)


def validate(conn):
    from .schema_contract import tokens as _tokens
    replacements = dict(legacy_order_guards()) if ordered_legacy(conn) else {}
    if bounded_basis(conn):
        replacements.update(bounded_basis_guards())
    for name, sql in definitions():
        sql = replacements.get(name,sql)
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise ValueError('Incompatible embedding file owner: ' + name)


def bounded_basis(conn):
    return (conn.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_changes'").fetchone() is not None
            and conn.execute("SELECT 1 FROM schema_changes WHERE version=32 "
                             "AND name='bounded_embedding_basis_guards'").fetchone() is not None)


def bounded_basis_guards():
    """A space already has one basis; compare its head instead of scanning it.

    Each inserted or updated row must match that head in both installed and
    file-backed storage. The invariant remains true after deletions too.
    """
    for kind in ('recording', 'artist'):
        files, plain = kind + '_embedding_files', kind + '_embeddings'
        for operation in ('insert', 'update'):
            for owner, other, name in (
                    (files, plain, files + '_file_basis_' + operation),
                    (plain, files, plain + '_file_basis_' + operation),
                    (files, files, files + '_basis_' + operation)):
                yield name, (f'CREATE TRIGGER {name} BEFORE {operation.upper()} ON {owner} '
                    f'WHEN EXISTS(SELECT 1 FROM (SELECT model_version,dimensions FROM {other} '
                    'WHERE space=NEW.space LIMIT 1) e '
                    'WHERE e.model_version<>NEW.model_version OR e.dimensions<>NEW.dimensions) '
                    "BEGIN SELECT RAISE(ABORT,'Mixed bases in an embedding space'); END")


def install_basis_guards(conn, context=None):
    for name, sql in bounded_basis_guards():
        conn.execute('DROP TRIGGER IF EXISTS ' + name)
        conn.execute(sql)


def validate_basis_guards(conn):
    from .schema_contract import tokens
    for name, sql in bounded_basis_guards():
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise ValueError('Incompatible bounded embedding basis guard: ' + name)


def ordered_legacy(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recording_embedding_legacy_order'").fetchone() is not None


def legacy_order_guards():
    for kind in ('recording','artist'):
        table, files, orders = kind+'_embeddings',kind+'_embedding_files',kind+'_embedding_legacy_order'
        order = ('coalesce((SELECT insertion_order FROM '+files+f' WHERE {kind}_id=NEW.{kind}_id AND space=NEW.space),'
            f'max(coalesce((SELECT max(rowid) FROM {table} WHERE rowid<>NEW.rowid),0),'
            f'coalesce((SELECT max(insertion_order) FROM {orders}),0),'
            f'coalesce((SELECT max(insertion_order) FROM {files}),0))+1)')
        yield kind+'_embedding_plain_insert', (f'CREATE TRIGGER {kind}_embedding_plain_insert AFTER INSERT ON {table} '
            f'BEGIN INSERT INTO {orders} VALUES (NEW.rowid,{order}); '
            f'DELETE FROM {files} WHERE {kind}_id=NEW.{kind}_id AND space=NEW.space; END')
        yield kind+'_embedding_plain_update', (f'CREATE TRIGGER {kind}_embedding_plain_update AFTER UPDATE ON {table} '
            f'BEGIN INSERT INTO {orders} VALUES (NEW.rowid,coalesce((SELECT insertion_order FROM {orders} '
            'WHERE row_id=OLD.rowid),OLD.rowid)) ON CONFLICT(row_id) DO UPDATE SET insertion_order=excluded.insertion_order; '
            f'DELETE FROM {orders} WHERE row_id=OLD.rowid AND OLD.rowid<>NEW.rowid; '
            f'DELETE FROM {files} WHERE {kind}_id=NEW.{kind}_id AND space=NEW.space; END')
        yield kind+'_embedding_plain_delete', (f'CREATE TRIGGER {kind}_embedding_plain_delete AFTER DELETE ON {table} '
            f'BEGIN DELETE FROM {orders} WHERE row_id=OLD.rowid; END')


def legacy_order_definitions():
    for kind in ('recording','artist'):
        table = kind+'_embedding_legacy_order'
        yield table, f'CREATE TABLE {table} (row_id INTEGER PRIMARY KEY,insertion_order INTEGER NOT NULL CHECK(insertion_order>0))'
        for owner in (table,kind+'_embedding_files'):
            name = 'ix_'+owner+'_order'
            yield name, f'CREATE INDEX {name} ON {owner}(insertion_order)'
    yield from legacy_order_guards()


def install_legacy_order_guards(conn):
    for name,sql in legacy_order_guards():
        conn.execute('DROP TRIGGER IF EXISTS '+name)
        conn.execute(sql)


def install_legacy_order(conn, context=None):
    for name,sql in legacy_order_definitions():
        if sql.startswith('CREATE TRIGGER'):
            conn.execute('DROP TRIGGER IF EXISTS '+name)
        conn.execute(sql)


def validate_legacy_order(conn):
    from .schema_contract import tokens as _tokens
    for name,sql in legacy_order_definitions():
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is None or _tokens(row[0])!=_tokens(sql):
            raise ValueError('Incompatible installed embedding order: '+name)


def directory(conn):
    return file_paths.directory(conn, 'embeddings')


def relative(kind, publication):
    from pathlib import Path
    if kind not in ('recording', 'artist') or not isinstance(publication, bytes) or len(publication) != 32:
        raise ValueError('Invalid embedding publication')
    digest = publication.hex()
    return Path(kind, digest[:2], digest + '.bin')


def legacy_relation(conn, kind):
    table = kind+'_embeddings'
    if ordered_legacy(conn):
        return (f'(SELECT e.*,coalesce(o.insertion_order,e.rowid) AS insertion_order FROM {table} e '
                f'LEFT JOIN {kind}_embedding_legacy_order o ON o.row_id=e.rowid)')
    return f'(SELECT *,rowid AS insertion_order FROM {table})'


def relation(conn, kind):
    """Metadata-only logical owner; never reads files to count or join subjects."""
    table = legacy_relation(conn,kind)
    if not available(conn):
        return f'(SELECT *,NULL AS publication,NULL AS position FROM {table})'
    return (f'(SELECT *,NULL AS publication,NULL AS position FROM {table} UNION ALL '
        f'SELECT {kind}_id,space,model_version,dimensions,NULL AS vector,built_at,insertion_order,publication,position '
        f'FROM {kind}_embedding_files)')


def load_many(conn, kind, rows):
    return _load_many(directory(conn), kind, rows)


def _load_many(root, kind, rows):
    """Read each selected chunk once, restoring the caller's original order."""
    result, chunks = [], {}
    for key, vector, dimensions, publication, position in rows:
        result.append([key, vector])
        if publication is not None:
            chunks.setdefault(publication, []).append((len(result)-1, dimensions, position))
    for publication, positions in chunks.items():
        path = root / relative(kind, publication)
        raw = fao.read(path, max_bytes=MAX_BYTES).bytes
        if hashlib.sha256(raw).digest() != publication or len(raw) < HEADER.size:
            raise fao.CorruptFile('Embedding chunk differs from its publication')
        magic, count, dimensions = HEADER.unpack(raw[:HEADER.size])
        if magic != MAGIC or not 0 < count <= MAX_ROWS or len(raw) != HEADER.size + count * dimensions * 4:
            raise fao.CorruptFile('Invalid embedding binary chunk')
        for index, expected, position in positions:
            if dimensions != expected or not 0 <= position < count:
                raise fao.CorruptFile('Embedding position differs from its head')
            offset = HEADER.size + position * dimensions * 4
            result[index][1] = raw[offset:offset + dimensions * 4]
    return [tuple(row) for row in result]


def prepare(conn, kind, dimensions, rows):
    return _prepare(directory(conn), kind, dimensions, rows)


def _prepare(root, kind, dimensions, rows):
    """Write immutable chunks while the caller holds its retained-reader fence.

    Return SQL metadata only. A rejected snapshot leaves collectible orphan
    chunks and never changes the previous serving references.
    """
    rows = list(rows)
    if type(dimensions) is not int or not 0 < dimensions <= (MAX_BYTES-HEADER.size)//4:
        raise ValueError('Embedding dimensions exceed the chunk byte budget')
    batch_rows = min(MAX_ROWS,(MAX_BYTES-HEADER.size)//(dimensions*4))
    result = []
    for offset in range(0, len(rows), batch_rows):
        batch = rows[offset:offset + batch_rows]
        if any(not isinstance(vector, bytes) or len(vector) != dimensions * 4 for _, vector, _, _ in batch):
            raise ValueError('Embedding bytes differ from the declared dimensions')
        raw = HEADER.pack(MAGIC, len(batch), dimensions) + b''.join(row[1] for row in batch)
        publication = hashlib.sha256(raw).digest()
        fao.write(root / relative(kind, publication), raw)
        for position, (subject, vector, built, order) in enumerate(batch):
            result.append((subject, dimensions, publication, position, order, built))
    return tuple(result)


def publish_prepared(conn, kind, space, version, rows):
    if not conn.in_transaction:
        raise RuntimeError('Embedding file publication requires a writer reservation')
    conn.executemany(f'DELETE FROM {kind}_embeddings WHERE {kind}_id=? AND space=?',
                     [(row[0], space) for row in rows])
    conn.executemany(f'INSERT INTO {kind}_embedding_files VALUES (?,?,?,?,?,?,?,?) '
        f'ON CONFLICT({kind}_id,space,model_version) DO UPDATE SET publication=excluded.publication,'
        'position=excluded.position,built_at=excluded.built_at',
        [(subject, space, version, dimensions, publication, position, order, built)
         for subject, dimensions, publication, position, order, built in rows])


def publish(conn, kind, space, version, dimensions, rows):
    """Rows are canonical (subject, exact bytes, built time, insertion order)."""
    if not conn.in_transaction:
        raise RuntimeError('Embedding file publication requires a writer reservation')
    publish_prepared(conn, kind, space, version, prepare(conn, kind, dimensions, rows))


def prepare_batch(conn, kind, limit=MAX_ROWS):
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise ValueError('Embedding batch must be 1 to 128')
    rows = conn.execute(f'SELECT insertion_order,{kind}_id,space,model_version,dimensions,vector,built_at '
                        f'FROM {legacy_relation(conn,kind)} ORDER BY insertion_order LIMIT ?', (limit,)).fetchall()
    groups = {}
    for row in rows:
        order, subject, space, version, dimensions, vector, built = tuple(row)
        groups.setdefault((space, version, dimensions), []).append((subject, vector, built, order))
    for (space, version, dimensions), values in groups.items():
        publish(conn, kind, space, version, dimensions, values)
        heads = []
        for subject, vector, built, order in values:
            head = conn.execute(f'SELECT {kind}_id,NULL,dimensions,publication,position FROM {kind}_embedding_files '
                f'WHERE {kind}_id=? AND space=? AND model_version=?', (subject, space, version)).fetchone()
            heads.append(head)
        if [vector for _, vector in load_many(conn, kind, heads)] != [row[1] for row in values]:
            raise fao.CorruptFile('Embedding conversion changed float32 bytes')
    return len(rows)


def references(conn):
    if available(conn):
        for kind in ('recording', 'artist'):
            for (publication,) in conn.execute(f'SELECT DISTINCT publication FROM {kind}_embedding_files'):
                yield directory(conn) / relative(kind, publication)


def collect(conn, now, limit=16):
    # The same immutable chunk/reference contract as musical profiles.
    from . import profile_files
    return profile_files.collect_unreferenced(conn, directory(conn),
        {'recording': 'recording_embedding_files', 'artist': 'artist_embedding_files'}, now, limit, '*/*/*.bin')
