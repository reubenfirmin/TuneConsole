"""Store one file-backed musical profile on each recording."""
from .. import group_preview_work, profile_files
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged


NAME = 'fold_recording_profile_storage'
MARKER = 'ix_recordings_musical_profile_version'
RETIRED = ('rec_musical_profiles', 'recording_profile_files')


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND "
        "(tbl_name IN ('recordings',?,?)) ORDER BY type,name", RETIRED)]


def _has_inline(conn):
    return conn.execute("SELECT 1 FROM rec_musical_profiles WHERE payload<>'' LIMIT 1").fetchone() is not None


def prepare(conn, version):
    while True:
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            if (conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone()
                    or not _has_inline(conn)):
                return
            moved = profile_files.prepare_batch(conn, 'recording')
            if not moved:
                raise MigrationError('An inline musical profile has no convertible identity')


def install(conn, _context):
    if _has_inline(conn):
        raise PreparationChanged()
    if conn.execute('''
        SELECT 1 FROM rec_musical_profiles p
        LEFT JOIN recordings r ON r.identity_key=p.identity_key
        LEFT JOIN recording_profile_files h ON h.recording_id=r.id
        LEFT JOIN profile_file_writes f ON f.id=h.file_id
        WHERE h.recording_id IS NULL OR (h.file_id IS NOT NULL AND
              (f.file_status IS NOT 'ready' OR f.kind IS NOT 'recording'
               OR f.content_digest IS NOT h.publication OR f.byte_size<h.byte_size))
        UNION ALL
        SELECT 1 FROM recording_profile_files h JOIN recordings r ON r.id=h.recording_id
        LEFT JOIN rec_musical_profiles p ON p.identity_key=r.identity_key WHERE p.identity_key IS NULL
        LIMIT 1
    ''').fetchone():
        raise MigrationError('A recording musical profile does not match its selected file')
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
        conn.execute(f'ALTER TABLE recordings ADD COLUMN {column}')
    conn.execute('''
        UPDATE recordings AS r SET musical_profile_version=p.version,musical_profile_digest=p.digest,
            musical_profile_prepared_at=p.prepared_at,musical_profile_publication=h.publication,
            musical_profile_size=h.byte_size,musical_profile_file_id=h.file_id
        FROM rec_musical_profiles p CROSS JOIN recording_profile_files h
        WHERE p.identity_key=r.identity_key AND h.recording_id=r.id
    ''')
    conn.execute(f'CREATE INDEX {MARKER} ON recordings(musical_profile_version,identity_key) '
                 'WHERE musical_profile_version IS NOT NULL')
    conn.execute('CREATE INDEX ix_recordings_musical_profile_file ON recordings(musical_profile_file_id) '
                 'WHERE musical_profile_file_id IS NOT NULL')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    conn.execute('''CREATE TRIGGER group_clock_recordings_musical_profile
        AFTER UPDATE OF musical_profile_version,musical_profile_digest,musical_profile_prepared_at ON recordings
        WHEN OLD.musical_profile_version IS NOT NEW.musical_profile_version
          OR OLD.musical_profile_digest IS NOT NEW.musical_profile_digest
          OR OLD.musical_profile_prepared_at IS NOT NEW.musical_profile_prepared_at
        BEGIN UPDATE group_preparation_state SET serial=serial+1 WHERE singleton=1; END''')
    conn.execute('''CREATE TRIGGER group_clock_recordings_musical_profile_delete AFTER DELETE ON recordings
        WHEN OLD.musical_profile_version IS NOT NULL
        BEGIN UPDATE group_preparation_state SET serial=serial+1 WHERE singleton=1; END''')
    group_preview_work.install_guards(conn, sources={'recordings'})
    conn.execute("UPDATE group_preview_work SET invalid=1 WHERE EXISTS (SELECT 1 FROM group_preview_dependencies "
                 "WHERE table_name='rec_musical_profiles')")


def validate(conn):
    if set(definitions(conn, (MARKER, *RETIRED))) != {MARKER}:
        raise MigrationError('Recording profile storage was not consolidated')
