"""Current explicit values and immutable file revision identities on uploads."""
from . import worker_settings

VIEW = 'current_runtime_choices'
COLUMN = 'runtime_metadata_choices'
INDEX = 'ix_track_sources_with_runtime_choices'
SERIAL = 'metadata_choice_id_high_water'
DETAILS = ('id', 'provider_id', 'revision', 'kind', 'text_value', 'number_value', 'genre_key')


def head(conn, entry_id, field_code):
    path = f'$.{field_code}'
    return conn.execute(f'''SELECT ({COLUMN}->?)->>0 AS id,({COLUMN}->?)->>2 AS revision
        FROM track_sources WHERE id=? AND json_type({COLUMN},?)='array' ''',
        (path, path, entry_id, path)).fetchone()


def next_identity(conn):
    latest = int(worker_settings.read(conn, SERIAL, '0'))
    worker_settings.save(conn, SERIAL, str(latest + 1))
    return latest + 1


def save(conn, entry_id, field_code, identity, provider, revision, kind, text, number, genre):
    path = f'$.{field_code}'
    conn.execute(f'''UPDATE track_sources SET {COLUMN}=json_set({COLUMN},?,json_array(
        COALESCE(({COLUMN}->?)->>0,?),?,?,?,?,
        CASE typeof(?) WHEN 'real' THEN json(printf('%!.26g',?)) ELSE ? END,?)) WHERE id=?''',
        (path, path, identity, provider, revision, kind, text, number, number, number, genre, entry_id))
    latest = int(worker_settings.read(conn, SERIAL, '0'))
    if identity > latest:
        worker_settings.save(conn, SERIAL, str(identity))


def relation(conn):
    """Open the prior file inventory during an explicit offline export."""
    installed = conn.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (VIEW,)).fetchone()
    return VIEW if installed else 'metadata_runtime_choices'
