"""Pending and ready embedding pack identities; SQL only."""
from yt_playlist.core.schema_contract import MigrationError, tokens
from yt_playlist.core.schema_inspection import definitions

SQL = '''CREATE TABLE embedding_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('recording','artist')),
    dimensions INTEGER NOT NULL CHECK(typeof(dimensions)='integer' AND dimensions>0),
    content_digest BLOB NOT NULL CHECK(typeof(content_digest)='blob' AND length(content_digest)=32),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 28 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''
COLUMN = 'file_id INTEGER REFERENCES embedding_file_writes(id)'


def head_definition(kind):
    spaces = {'recording': "'collaborative','content_library','content_discovery'",
              'artist': "'collaborative','content'"}[kind]
    table = kind + '_embedding_files'
    return (f'CREATE TABLE {table} ({kind}_id INTEGER NOT NULL REFERENCES {kind}s(id),'
        f'space TEXT NOT NULL CHECK(space IN ({spaces})),model_version TEXT NOT NULL CHECK(length(model_version)>0),'
        "dimensions INTEGER NOT NULL CHECK(typeof(dimensions)='integer' AND dimensions>0),publication BLOB NOT NULL CHECK(length(publication)=32),"
        'position INTEGER NOT NULL CHECK(position>=0 AND position<128),insertion_order INTEGER NOT NULL CHECK(insertion_order>0),'
        f"built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('real','integer') AND abs(built_at)<=1.7976931348623157e308)),PRIMARY KEY({kind}_id,space,model_version))")


def native_definition(sql):
    return sql.replace(',PRIMARY KEY(', ',' + COLUMN + ',PRIMARY KEY(')


def native(conn):
    sql = definitions(conn, ('recording_embedding_files',)).get('recording_embedding_files', '')
    return 'file_id' in tokens(sql)


def install(conn, context=None):
    actual = definitions(conn, ('embedding_file_writes', 'recording_embedding_files', 'artist_embedding_files'))
    if 'embedding_file_writes' not in actual:
        conn.execute(SQL)
    elif tokens(actual['embedding_file_writes']) != tokens(SQL):
        raise MigrationError('Incompatible embedding file identities')
    for kind in ('recording', 'artist'):
        table = kind + '_embedding_files'
        if 'file_id' not in tokens(actual[table]):
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {COLUMN}')


def validate(conn):
    expected = {'embedding_file_writes': SQL}
    expected.update((kind + '_embedding_files', native_definition(head_definition(kind)))
                    for kind in ('recording', 'artist'))
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible embedding file identity owner: ' + name)


def reserve(conn, records):
    install(conn)
    return [conn.execute('INSERT INTO embedding_file_writes('
        'kind,dimensions,content_digest,byte_size,file_status) VALUES (?,?,?,?,\'pending\')',
        (record['kind'], record['dimensions'], record['digest'], record['size'])).lastrowid
        for record in records]


def complete(conn, kind, rows):
    packs = {}
    for subject, dimensions, digest, position, order, built, identity in rows:
        if type(identity) is not int or identity <= 0:
            raise ValueError('Invalid embedding file reservation')
        pack = packs.setdefault(identity, (dimensions, digest, []))
        if pack[:2] != (dimensions, digest):
            raise ValueError('Embedding file reservation has conflicting contents')
        pack[2].append(position)
    for identity, (dimensions, digest, positions) in packs.items():
        if sorted(positions) != list(range(len(positions))) or not 0 < len(positions) <= 128:
            raise ValueError('Embedding pack positions must be complete and unique')
        changed = conn.execute("UPDATE embedding_file_writes SET file_status='ready' "
            'WHERE id=? AND kind=? AND dimensions=? AND content_digest=? AND byte_size=? '
            "AND file_status='pending'", (identity, kind, dimensions, digest,
                                        24 + len(positions) * dimensions * 4)).rowcount
        if changed != 1:
            raise ValueError('Embedding file reservation changed before completion')
