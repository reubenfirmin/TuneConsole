"""SQL reservations for immutable, bounded prediction provenance packs."""
from yt_playlist.core.schema_contract import MigrationError, tokens
from yt_playlist.core.schema_inspection import definitions

HEAD_SQL = '''CREATE TABLE prediction_provenance (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id) ON DELETE CASCADE,
    input_fingerprint BLOB NOT NULL CHECK(length(input_fingerprint)=32),
    byte_size INTEGER NOT NULL CHECK(byte_size>0),
    state TEXT NOT NULL CHECK(state IN ('ready','expired'))
)'''
SQL = '''CREATE TABLE provenance_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_digest BLOB NOT NULL CHECK(typeof(content_digest)='blob' AND length(content_digest)=32),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''
COLUMN = 'file_id INTEGER REFERENCES provenance_file_writes(id)'
INDEX = 'CREATE INDEX ix_prediction_provenance_file ON prediction_provenance(file_id,state)'
GUARD = ('CREATE TRIGGER prediction_provenance_file_immutable BEFORE UPDATE OF file_id ON prediction_provenance '
         "BEGIN SELECT RAISE(ABORT,'Calculation provenance file is immutable'); END")


def native_definition(sql):
    return sql.replace("state IN ('ready','expired'))", "state IN ('ready','expired')),\n    " + COLUMN)


def native(conn):
    return any(row[1] == 'file_id' for row in conn.execute('PRAGMA main.table_xinfo(prediction_provenance)'))


def install(conn, context=None):
    actual = definitions(conn, ('provenance_file_writes', 'prediction_provenance'))
    if 'provenance_file_writes' not in actual:
        conn.execute(SQL)
    elif tokens(actual['provenance_file_writes']) != tokens(SQL):
        raise MigrationError('Incompatible prediction provenance file identities')
    if 'file_id' not in tokens(actual['prediction_provenance']):
        conn.execute('ALTER TABLE prediction_provenance ADD COLUMN ' + COLUMN)
    for name, sql in (('ix_prediction_provenance_file', INDEX), ('prediction_provenance_file_immutable', GUARD)):
        installed = definitions(conn, (name,))
        if name not in installed:
            conn.execute(sql)
        elif tokens(installed[name]) != tokens(sql):
            raise MigrationError('Incompatible prediction provenance file owner: ' + name)


def validate(conn):
    expected = {'provenance_file_writes': SQL, 'prediction_provenance': native_definition(HEAD_SQL),
                'ix_prediction_provenance_file': INDEX, 'prediction_provenance_file_immutable': GUARD}
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible prediction provenance file owner: ' + name)


def reserve(conn, records):
    install(conn)
    return [conn.execute("INSERT INTO provenance_file_writes(content_digest,byte_size,file_status) "
        "VALUES (?,?,'pending')", (record['digest'], record['size'])).lastrowid for record in records]


def complete(conn, records):
    for record in records:
        changed = conn.execute("UPDATE provenance_file_writes SET file_status='ready' WHERE id=? "
            "AND content_digest=? AND byte_size=? AND file_status='pending'",
            (record['file_id'], record['digest'], record['size'])).rowcount
        if changed != 1:
            raise ValueError('Prediction provenance file reservation changed before completion')
