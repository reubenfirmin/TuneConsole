"""Keep Mix Tape requests and preparation checkpoints on their existing rows."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from ..recording_selection import READY


NAME = 'own_mix_worker_state'
OWNERS = ('content_full_checkpoint', 'home_genre_requests')
REQUEST_KEY = 'home_mix_vibe_request'
SELECTION_KEY = 'recording_mix_selection_prepared'
RANKING = (
    ('rec_candidate_preferences', 'ranking_fingerprint TEXT'),
    ('rec_candidates_expire_at', 'ranking_expires_at REAL'),
    ('rec_candidates_checked_at', 'ranking_checked_at REAL'),
    ('rec_candidates_checked_group', 'ranking_checked_group TEXT'),
)


def predecessor_objects(conn):
    schema = definitions(conn, OWNERS)
    return [schema[owner] for owner in OWNERS]


def install(conn, _context):
    conn.execute('ALTER TABLE home_genre_requests ADD COLUMN vibe_request_id TEXT')
    conn.execute('ALTER TABLE home_genre_requests ADD COLUMN vibe_preset TEXT')
    conn.execute("""ALTER TABLE home_genre_requests ADD COLUMN vibe_phase TEXT CHECK(
        (vibe_request_id IS NULL AND vibe_preset IS NULL AND vibe_phase IS NULL)
        OR (vibe_request_id IS NOT NULL AND vibe_phase IS NOT NULL
            AND vibe_phase IN ('pending','waiting','ready','failed'))
    )""")
    conn.execute("""
        INSERT INTO home_genre_requests(id,vibe_request_id,vibe_preset,vibe_phase)
        SELECT 1,json_extract(value,'$.id'),json_extract(value,'$.vibe'),json_extract(value,'$.phase')
        FROM settings WHERE key=? AND value<>''
        ON CONFLICT(id) DO UPDATE SET vibe_request_id=excluded.vibe_request_id,
            vibe_preset=excluded.vibe_preset,vibe_phase=excluded.vibe_phase
    """, (REQUEST_KEY,))

    for key, declaration in RANKING:
        column, affinity = declaration.split()
        selection = 'value'
        if affinity == 'REAL':
            declaration += f" CHECK({column} IS NULL OR (typeof({column})='real' AND abs({column})<=1.7976931348623157e308))"
            selection = "nullif(value,'')"
        conn.execute(f'ALTER TABLE content_full_checkpoint ADD COLUMN {declaration}')
        conn.execute(f'UPDATE content_full_checkpoint SET {column}=(SELECT {selection} FROM settings WHERE key=?) '
                     'WHERE singleton=1', (key,))

    conn.execute('ALTER TABLE content_full_checkpoint ADD COLUMN recording_selection_ready '
                 'INTEGER NOT NULL DEFAULT 0 CHECK(recording_selection_ready IN (0,1))')
    conn.execute('UPDATE content_full_checkpoint SET recording_selection_ready=1 WHERE singleton=1 '
                 'AND EXISTS(SELECT 1 FROM settings WHERE key=?)', (SELECTION_KEY,))
    conn.executemany('DELETE FROM settings WHERE key=?',
                     ((key,) for key in (REQUEST_KEY, SELECTION_KEY, *(key for key, _ in RANKING))))


def validate(conn, *, home_settings=False):
    schema = definitions(conn, OWNERS)
    selection = ('recording_selection_ready' in tokens(schema['content_full_checkpoint'])
                 if 'content_full_checkpoint' in schema else
                 conn.execute('SELECT 1 FROM settings WHERE key=?', (READY,)).fetchone() is not None)
    if ((not home_settings and 'vibe_request_id' not in tokens(schema.get('home_genre_requests')))
            or not selection):
        raise MigrationError('Mix worker state columns are missing')
