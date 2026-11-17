"""SQL receipts for immutable annotation files; callers own transactions."""
from yt_playlist.core.annotation_paths import record_id
from yt_playlist.core.schema_contract import MigrationError, tokens

SQL = '''CREATE TABLE annotation_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL CHECK(length(run_id)>0),
    taxonomy_version TEXT NOT NULL CHECK(length(taxonomy_version)>0),
    revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 4194304),
    annotation_id INTEGER UNIQUE REFERENCES audio_annotations(id),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready')),
    CHECK((file_status='pending' AND annotation_id IS NULL) OR
          (file_status='ready' AND annotation_id IS NOT NULL))
)'''

LEGACY_CHECK = "CHECK(substr(model_output_filename,-69)=model_output_digest||'.json')"
NATIVE_CHECK = 'CHECK(length(model_output_filename)>0)'


def install(conn, context=None):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='annotation_file_writes'").fetchone()
    if row is None:
        conn.execute(SQL)
    elif tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible annotation file identities')


def validate(conn):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='annotation_file_writes'").fetchone()
    if row is None or tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible annotation file identities')


def native(conn):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='audio_annotations'").fetchone()
    if row is None:
        return False
    actual, expected = tokens(row[0]), tokens(NATIVE_CHECK)
    return any(actual[index:index + len(expected)] == expected for index in range(len(actual) - len(expected) + 1))


def normalize_empty(conn):
    """The offline compiler owns an empty schema with its guards suspended."""
    if native(conn):
        return
    if conn.execute('SELECT 1 FROM audio_annotations LIMIT 1').fetchone():
        raise ValueError('Annotation filename conversion requires an empty rebuild destination')
    sql = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='audio_annotations'").fetchone()[0]
    if sql.count(LEGACY_CHECK) != 1:
        raise MigrationError('Unrecognized installed annotation filename constraint')
    indexes = [row[0] for row in conn.execute("SELECT sql FROM main.sqlite_schema WHERE type='index' "
        "AND tbl_name='audio_annotations' AND sql IS NOT NULL")]
    conn.execute('DROP TABLE audio_annotations')
    conn.execute(sql.replace(LEGACY_CHECK, NATIVE_CHECK))
    for statement in indexes:
        conn.execute(statement)


def reserve(conn, run_id, taxonomy_version, revision, integrity):
    if not native(conn):
        raise ValueError('Annotation storage requires the offline database cutover')
    install(conn)
    return conn.execute('INSERT INTO annotation_file_writes('
        'run_id,taxonomy_version,revision,content_digest,byte_size,file_status) '
        "VALUES (?,?,?,?,?,'pending')", (run_id, taxonomy_version, revision, *integrity)).lastrowid


def complete(conn, identity, run_id, taxonomy_version, revision, reference, annotation_id):
    if record_id(reference[0]) != identity:
        raise ValueError('Annotation filename does not match its reserved identity')
    changed = conn.execute("UPDATE annotation_file_writes SET file_status='ready',annotation_id=? "
        'WHERE id=? AND run_id=? AND taxonomy_version=? AND revision=? AND content_digest=? '
        "AND byte_size=? AND file_status='pending' AND annotation_id IS NULL",
        (annotation_id, identity, run_id, taxonomy_version, revision, *reference[1:])).rowcount
    if changed != 1:
        raise ValueError('Annotation file reservation changed before completion')
