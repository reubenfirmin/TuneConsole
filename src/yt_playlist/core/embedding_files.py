"""Exact binary embedding chunks and typed canonical subject heads (#694)."""
from yt_playlist.core import file_paths
import hashlib
import struct
from contextlib import contextmanager
from pathlib import Path

from . import fao, file_collection
from .embedding_paths import record_path, record_id
from .file_receipts import embedding_head_definition, has_native_embedding_heads, native_embedding_definition
from yt_playlist.repos.embedding_file_references import publish_prepared

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

    def garbage(self, before, *, limit=16):
        return _garbage(self, before, limit=limit)

    @staticmethod
    def remove(path, info):
        return fao.delete(path, expected=info.identity, missing_ok=True)


def definitions():
    for kind in ('recording', 'artist'):
        table = kind + '_embedding_files'
        yield table, embedding_head_definition(kind)
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
    from .schema_inspection import definitions as read_definitions
    replacements = dict(legacy_order_guards()) if ordered_legacy(conn) else {}
    if bounded_basis(conn):
        replacements.update(bounded_basis_guards())
    expected = tuple(definitions())
    actual = read_definitions(conn, (name for name, _ in expected))
    for name, sql in expected:
        sql = replacements.get(name,sql)
        if name in ('recording_embedding_files', 'artist_embedding_files') and 'file_id' in _tokens(actual.get(name) or ''):
            sql = native_embedding_definition(sql)
        if actual.get(name) is None or _tokens(actual[name]) != _tokens(sql):
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
    from .schema_inspection import definitions as read_definitions
    expected = tuple(bounded_basis_guards())
    actual = read_definitions(conn, (name for name, _ in expected))
    for name, sql in expected:
        if actual.get(name) is None or tokens(actual[name]) != tokens(sql):
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
    from .schema_inspection import definitions as read_definitions
    expected = tuple(legacy_order_definitions())
    actual = read_definitions(conn, (name for name, _ in expected))
    for name,sql in expected:
        if actual.get(name) is None or _tokens(actual[name]) != _tokens(sql):
            raise ValueError('Incompatible installed embedding order: '+name)


def inline_order_guards(kind):
    table, files = kind + '_embeddings', kind + '_embedding_files'
    owner = f'{kind}_id=NEW.{kind}_id AND space=NEW.space'
    yield kind + '_embedding_plain_insert', (
        f'CREATE TRIGGER {kind}_embedding_plain_insert AFTER INSERT ON {table} BEGIN '
        f'UPDATE {table} SET insertion_order=coalesce((SELECT insertion_order FROM {files} WHERE {owner}), '
        f'max(coalesce((SELECT max(insertion_order) FROM {table}),0), '
        f'coalesce((SELECT max(insertion_order) FROM {files}),0))+1) '
        'WHERE rowid=NEW.rowid AND NEW.insertion_order IS NULL; '
        f'DELETE FROM {files} WHERE {owner}; END')
    yield kind + '_embedding_plain_update', (
        f'CREATE TRIGGER {kind}_embedding_plain_update AFTER UPDATE OF {kind}_id,space,model_version,'
        f'dimensions,vector,built_at ON {table} BEGIN DELETE FROM {files} WHERE {owner}; END')


def fold_legacy_order(conn, _context):
    for kind in ('recording', 'artist'):
        table, order = kind + '_embeddings', kind + '_embedding_legacy_order'
        for event in ('insert', 'update', 'delete'):
            conn.execute(f'DROP TRIGGER {kind}_embedding_plain_{event}')
        conn.execute(f'ALTER TABLE {table} ADD COLUMN insertion_order INTEGER CHECK(insertion_order>0)')
        conn.execute(f'UPDATE {table} AS e SET insertion_order=coalesce('
                     f'(SELECT o.insertion_order FROM {order} o WHERE o.row_id=e.rowid),e.rowid)')
        conn.execute(f'CREATE INDEX ix_{table}_order ON {table}(insertion_order)')
        for _, sql in inline_order_guards(kind):
            conn.execute(sql)
        conn.execute(f'DROP TABLE {order}')


def validate_folded_order(conn):
    if ordered_legacy(conn) or conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='artist_embedding_legacy_order'").fetchone():
        raise ValueError('Obsolete embedding order storage is still installed')


def directory(conn):
    return file_paths.directory(conn, 'embeddings')


def relative(kind, publication):
    from pathlib import Path
    if isinstance(publication, tuple) and len(publication) == 2:
        digest, identity = publication
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise ValueError('Invalid embedding integrity digest')
        return record_path(kind, identity)
    if kind not in ('recording', 'artist') or not isinstance(publication, bytes) or len(publication) != 32:
        raise ValueError('Invalid embedding publication')
    digest = publication.hex()
    return Path(kind, digest[:2], digest + '.bin')


def relation(kind):
    """Return the fixed SQL table name for an embedding subject kind."""
    if kind not in ('recording', 'artist'):
        raise ValueError('Invalid embedding kind')
    return f'{kind}_embedding_files'


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
        digest = publication[0] if isinstance(publication, tuple) else publication
        raw = fao.read(path, max_bytes=MAX_BYTES).bytes
        if hashlib.sha256(raw).digest() != digest or len(raw) < HEADER.size:
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


def _encoded(dimensions, rows):
    rows = list(rows)
    if type(dimensions) is not int or not 0 < dimensions <= (MAX_BYTES-HEADER.size)//4:
        raise ValueError('Embedding dimensions exceed the chunk byte budget')
    batch_rows = min(MAX_ROWS,(MAX_BYTES-HEADER.size)//(dimensions*4))
    for offset in range(0, len(rows), batch_rows):
        batch = tuple(rows[offset:offset + batch_rows])
        if any(not isinstance(vector, bytes) or len(vector) != dimensions * 4 for _, vector, _, _ in batch):
            raise ValueError('Embedding bytes differ from the declared dimensions')
        raw = HEADER.pack(MAGIC, len(batch), dimensions) + b''.join(row[1] for row in batch)
        yield batch, raw


def _prepare(root, kind, dimensions, rows):
    """Publish immutable packs addressed by their content digest."""
    result = []
    for batch, raw in _encoded(dimensions, rows):
        publication = hashlib.sha256(raw).digest()
        fao.write(root / relative(kind, publication), raw)
        for position, (subject, vector, built, order) in enumerate(batch):
            result.append((subject, dimensions, publication, position, order, built))
    return tuple(result)


def publish(conn, kind, space, version, dimensions, rows):
    """Rows are canonical (subject, exact bytes, built time, insertion order)."""
    if not conn.in_transaction:
        raise RuntimeError('Embedding file publication requires a writer reservation')
    publish_prepared(conn, kind, space, version, prepare(conn, kind, dimensions, rows))


def prepare_batch(conn, kind, limit=MAX_ROWS):
    from yt_playlist.controllers.embedding_preparation import EmbeddingPreparation
    from yt_playlist.repos.embedding_file_references import EmbeddingSources
    return EmbeddingPreparation(EmbeddingSources(conn), EmbeddingFiles(directory(conn))).prepare(kind, limit)


def references(conn):
    if available(conn):
        identity = 'file_id' if has_native_embedding_heads(conn) else 'NULL'
        for kind in ('recording', 'artist'):
            for publication, file_id in conn.execute(f'SELECT DISTINCT publication,{identity} FROM {kind}_embedding_files'):
                yield directory(conn) / relative(kind, (publication, file_id) if file_id is not None else publication)


@contextmanager
def _garbage(files, before, *, limit):
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError('Embedding collection budget must be 1 to 256')
    def shard(name):
        return len(name) == 2 and all(c in '0123456789abcdef' for c in name)
    with file_collection.collection(files, files.root, name='.collect.json', valid_shard=shard,
            prefixes=('recording', 'artist', 'recording/by-id', 'artist/by-id')) as work, \
            file_collection.collector(files.root) as admitted:
        if not admitted:
            yield iter(())
            return
        def candidates():
            for _ in range(limit):
                path = work.next()
                if path is None:
                    continue
                identity = record_id(path)
                parts = path.relative_to(files.root).parts
                if identity is not None:
                    reference = identity
                elif (len(parts) == 3 and path.suffix == '.bin' and len(path.stem) == 64
                        and shard(path.parent.name) and path.stem[:2] == path.parent.name
                        and all(c in '0123456789abcdef' for c in path.stem)):
                    reference = bytes.fromhex(path.stem)
                else:
                    continue
                info = fao.read(path, max_bytes=MAX_BYTES).info
                if info.ctime_ns / 1e9 <= before:
                    yield parts[0], reference, path, info
        yield candidates()
        work.save()
