"""SQL identities for the finite recommendation output families.

The calling repository owns its transaction. These functions never access files
or open a transaction.
"""
from yt_playlist.core.document_paths import record_id
from yt_playlist.core.schema_contract import MigrationError, tokens

OWNERS = frozenset({'prepared_outputs', 'album_note_outputs', 'listening_output_files', 'home_display_files',
                   'recommendation_report_files', 'model_pair_files', 'radio_seed_output', 'model_autotune_output'})
SQL = '''CREATE TABLE recommendation_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_owner TEXT NOT NULL,
    surface TEXT NOT NULL,
    chunk_key TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 1048576),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''


def install(conn, context=None):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='recommendation_file_writes'").fetchone()
    if row is None:
        conn.execute(SQL)
    elif tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible recommendation file identities')


def validate(conn):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='recommendation_file_writes'").fetchone()
    if row is None or tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible recommendation file identities')


def reserve(conn, owner, surface, chunk_key, integrity):
    if owner not in OWNERS:
        raise ValueError('Unknown recommendation file owner')
    # Continuing installed-format writes need only this empty SQL owner; no
    # files or history are converted when regular admission is still deferred.
    install(conn)
    return conn.execute('INSERT INTO recommendation_file_writes '
        '(output_owner,surface,chunk_key,content_digest,byte_size,file_status) VALUES (?,?,?,?,?,\'pending\')',
        (owner, surface, chunk_key, *integrity)).lastrowid


def complete(conn, identity, owner, surface, chunk_key, reference):
    if record_id(reference[0]) != identity:
        raise ValueError('Recommendation file reservation changed before completion')
    changed = conn.execute("UPDATE recommendation_file_writes SET file_status='ready' "
        "WHERE id=? AND output_owner=? AND surface=? AND chunk_key=? AND content_digest=? AND byte_size=? "
        "AND file_status='pending'", (identity, owner, surface, chunk_key, *reference[1:])).rowcount
    if changed != 1:
        raise ValueError('Recommendation file reservation changed before completion')
