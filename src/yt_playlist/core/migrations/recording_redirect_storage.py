"""Put redirect target and correction evidence on the retired recording."""
from .. import group_preview_work
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'fold_recording_redirect_storage'
MARKER = 'ix_recordings_redirect_target'
RETIRED = 'recording_redirects'
OWNER_GUARDS = tuple(f'{table}_owner_{event}'
    for table in ('prepared_recording_inputs', 'recording_embedding_files')
    for event in ('insert', 'update'))
SUBJECT = 'rec_served_impressions_subject'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        'SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND '
        '(tbl_name IN (?,?) OR name IN (?,?,?,?,?)) ORDER BY type,name',
        ('recordings', RETIRED, *OWNER_GUARDS, SUBJECT))]


def install(conn, _context):
    rows = list(conn.execute('SELECT retired_id,recording_id,source,evidence FROM recording_redirects'))
    targets = {retired: target for retired, target, _, _ in rows}
    resolved = []
    for retired, target, source, evidence in rows:
        seen = {retired}
        while target in targets:
            if target in seen:
                raise MigrationError('Cyclic recording redirects')
            seen.add(target)
            target = targets[target]
        resolved.append((target, source, evidence, retired))
    conn.execute('ALTER TABLE recordings ADD COLUMN redirect_recording_id INTEGER REFERENCES recordings(id) '
                 'CHECK(redirect_recording_id<>id)')
    conn.execute('ALTER TABLE recordings ADD COLUMN redirect_source TEXT')
    conn.execute('ALTER TABLE recordings ADD COLUMN redirect_evidence TEXT '
                 'CHECK((redirect_recording_id IS NULL AND redirect_source IS NULL AND redirect_evidence IS NULL) OR '
                 '(redirect_recording_id IS NOT NULL AND redirect_source IS NOT NULL AND redirect_evidence IS NOT NULL))')
    conn.executemany('UPDATE recordings SET redirect_recording_id=?,redirect_source=?,redirect_evidence=? WHERE id=?',
                     resolved)
    conn.execute(f'CREATE INDEX {MARKER} ON recordings(redirect_recording_id) WHERE redirect_recording_id IS NOT NULL')
    for name, sql in definitions(conn, (*OWNER_GUARDS, SUBJECT)).items():
        conn.execute(f'DROP TRIGGER {name}')
        sql = sql.replace('SELECT 1 FROM recording_redirects WHERE retired_id=NEW.recording_id',
                          'SELECT 1 FROM recordings WHERE id=NEW.recording_id AND redirect_recording_id IS NOT NULL')
        sql = sql.replace('SELECT recording_id FROM recording_redirects WHERE retired_id=',
                          'SELECT redirect_recording_id FROM recordings WHERE id=')
        conn.execute(sql)
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute('''CREATE TRIGGER retire_recording_outputs AFTER UPDATE OF redirect_recording_id ON recordings
        WHEN OLD.redirect_recording_id IS NULL AND NEW.redirect_recording_id IS NOT NULL BEGIN
        DELETE FROM prepared_recording_inputs WHERE recording_id=NEW.id;
        DELETE FROM recording_embedding_files WHERE recording_id=NEW.id;
        END''')
    group_preview_work.install_guards(conn, sources={'recordings'})
    conn.execute("UPDATE group_preview_work SET invalid=1 WHERE EXISTS (SELECT 1 FROM group_preview_dependencies "
                 "WHERE table_name='recording_redirects')")


def validate(conn):
    if set(definitions(conn, (MARKER, RETIRED))) != {MARKER}:
        raise MigrationError('Recording redirects were not consolidated')
