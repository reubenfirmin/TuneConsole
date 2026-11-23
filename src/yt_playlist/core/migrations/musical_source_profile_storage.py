"""Keep one prepared musical profile and its file on each provider source."""
from .. import profile_files
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged
from .vibe_model_schema import REVISION


NAME = 'fold_musical_source_profile_storage'
MARKER = 'ix_rec_musical_sources_musical_profile_version'
RETIRED = ('source_profile_files', 'rec_musical_source_profiles')


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND "
        "tbl_name IN ('rec_musical_sources',?,?) ORDER BY type,name", RETIRED)]


def _has_inline(conn):
    return conn.execute("SELECT 1 FROM rec_musical_source_profiles WHERE payload<>'' LIMIT 1").fetchone() is not None


def prepare(conn, version):
    while True:
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            if (conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone()
                    or not _has_inline(conn)):
                return
            if not profile_files.prepare_batch(conn, 'source'):
                raise MigrationError('An inline source profile has no convertible identity')


def install(conn, _context):
    if _has_inline(conn):
        raise PreparationChanged()
    if conn.execute('''
        SELECT 1 FROM rec_musical_source_profiles p
        LEFT JOIN rec_musical_sources s ON s.source_key=p.source_key
        LEFT JOIN source_profile_files h ON h.source_key=p.source_key
        LEFT JOIN profile_file_writes f ON f.id=h.file_id
        WHERE s.source_key IS NULL OR h.source_key IS NULL OR (h.file_id IS NOT NULL AND
              (f.file_status IS NOT 'ready' OR f.kind IS NOT 'source'
               OR f.content_digest IS NOT h.publication OR f.byte_size<h.byte_size))
        LIMIT 1
    ''').fetchone():
        raise MigrationError('A musical source profile is missing its provider source or selected file')

    for column in (
        'musical_profile_version TEXT', 'musical_profile_digest TEXT', 'musical_profile_prepared_at REAL',
        'musical_profile_publication BLOB', 'musical_profile_size INTEGER',
        '''musical_profile_file_id INTEGER REFERENCES profile_file_writes(id)
           CHECK((musical_profile_version IS NULL AND musical_profile_digest IS NULL
               AND musical_profile_prepared_at IS NULL AND musical_profile_publication IS NULL
               AND musical_profile_size IS NULL AND musical_profile_file_id IS NULL)
             OR (musical_profile_version IS NOT NULL AND musical_profile_digest IS NOT NULL
               AND musical_profile_prepared_at IS NOT NULL AND typeof(musical_profile_publication)='blob'
               AND length(musical_profile_publication)=32 AND typeof(musical_profile_size)='integer'
               AND musical_profile_size>0))''',
    ):
        conn.execute(f'ALTER TABLE rec_musical_sources ADD COLUMN {column}')
    conn.execute('''
        UPDATE rec_musical_sources AS s SET musical_profile_version=p.version,musical_profile_digest=p.digest,
            musical_profile_prepared_at=p.prepared_at,musical_profile_publication=h.publication,
            musical_profile_size=h.byte_size,musical_profile_file_id=h.file_id
        FROM rec_musical_source_profiles p JOIN source_profile_files h USING(source_key)
        WHERE s.source_key=p.source_key
    ''')
    conn.execute(f'CREATE INDEX {MARKER} ON rec_musical_sources(musical_profile_version,source_key) '
                 'WHERE musical_profile_version IS NOT NULL')
    conn.execute('CREATE INDEX ix_rec_musical_sources_musical_profile_file ON '
                 'rec_musical_sources(musical_profile_file_id) WHERE musical_profile_file_id IS NOT NULL')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')

    for operation, condition in (
        ('INSERT', 'NEW.musical_profile_version IS NOT NULL'),
        ('UPDATE OF musical_profile_digest,musical_profile_version',
         'NEW.musical_profile_digest IS NOT OLD.musical_profile_digest '
         'OR NEW.musical_profile_version IS NOT OLD.musical_profile_version'),
        ('DELETE', 'OLD.musical_profile_version IS NOT NULL'),
    ):
        name = operation.split()[0].lower()
        conn.execute(f'''CREATE TRIGGER rec_musical_sources_joint_profile_{name}
            AFTER {operation} ON rec_musical_sources WHEN {condition}
            BEGIN INSERT INTO settings(key,value) VALUES ('{REVISION}','1')
                ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT); END''')


def validate(conn):
    if set(definitions(conn, (MARKER, *RETIRED))) != {MARKER}:
        raise MigrationError('Musical source profile storage was not consolidated')
