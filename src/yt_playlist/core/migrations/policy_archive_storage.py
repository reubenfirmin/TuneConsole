"""Keep archive files on their policy and record each impression role once."""
from ..policy_history_read import ARCHIVE_HEAD, NEXT_ARCHIVE_FILE
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table


NAME = 'consolidate_policy_archive_storage'
HISTORY = 'recommendation_policy_history'
LINKS = 'recommendation_impression_policies'
MARKER = 'ix_policy_history_archive_file'
RETIRED = ('policy_file_writes', 'policy_history_archive', 'policy_history_files', 'historical_impression_policies')
OWNERS = (HISTORY, LINKS, *RETIRED)
CAPTURE = 'recommendation_impression_policy_capture'
IMMUTABLE = 'recommendation_impression_link_immutable'
RETAINED = 'recommendation_impression_link_retained'


def predecessor_objects(conn):
    placeholders = ','.join('?' for _ in OWNERS)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM sqlite_schema WHERE (tbl_name IN ({placeholders}) OR name=?) AND sql IS NOT NULL '
        'ORDER BY type,name', (*OWNERS, CAPTURE))]


def install(conn, _context):
    _move_archive(conn)
    _merge_links(conn)
    conn.execute('DROP TABLE policy_file_writes')


def _move_archive(conn):
    conn.execute(f'ALTER TABLE {HISTORY} ADD COLUMN archive_file_id INTEGER CHECK(archive_file_id>0)')
    conn.execute(f'ALTER TABLE {HISTORY} ADD COLUMN archive_digest TEXT '
                 "CHECK(archive_digest IS NULL OR (length(archive_digest)=64 AND archive_digest NOT GLOB '*[^0-9a-f]*'))")
    conn.execute(f'ALTER TABLE {HISTORY} ADD COLUMN archive_byte_size INTEGER '
                 'CHECK((archive_file_id IS NULL AND archive_digest IS NULL AND archive_byte_size IS NULL) OR '
                 "(state='archived' AND archive_file_id IS NOT NULL AND archive_digest IS NOT NULL "
                 "AND typeof(archive_byte_size)='integer' AND archive_byte_size BETWEEN 1 AND 8388608))")
    conn.execute(f'UPDATE {HISTORY} AS h SET (archive_file_id,archive_digest,archive_byte_size)='
                 '(SELECT f.id,f.content_digest,f.byte_size FROM policy_history_files f '
                 "WHERE f.version=h.version AND f.file_status='ready' ORDER BY f.id DESC LIMIT 1) "
                 "WHERE h.state='archived'")
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON {HISTORY}(archive_file_id) WHERE archive_file_id IS NOT NULL')
    conn.execute('INSERT INTO settings(key,value) SELECT ?,lower(hex(publication)) FROM policy_history_archive WHERE id=1',
                 (ARCHIVE_HEAD,))
    conn.execute("INSERT INTO settings(key,value) SELECT ?,seq FROM sqlite_sequence WHERE name='policy_history_files'",
                 (NEXT_ARCHIVE_FILE,))
    conn.execute('DROP TABLE policy_history_files')
    conn.execute('DROP TABLE policy_history_archive')


def _merge_links(conn):
    old = 'historical_impression_policies'
    if conn.execute(f'SELECT 1 FROM {LINKS} r JOIN {old} h USING(impression_id,kind) LIMIT 1').fetchone():
        raise MigrationError('An impression policy role has two owners')
    affected = {name: sql for name, sql in conn.execute(
        'SELECT name,sql FROM sqlite_schema WHERE tbl_name=? AND type IN (\'index\',\'trigger\') AND sql IS NOT NULL',
        (LINKS,),
    )}
    capture = definitions(conn, (CAPTURE,))[CAPTURE]
    conn.execute(f'DROP TRIGGER {CAPTURE}')
    conn.execute('DROP TRIGGER historical_impression_matches')
    _replace_table(conn, LINKS, f'''CREATE TABLE {LINKS} (
        impression_id INTEGER NOT NULL REFERENCES rec_served_impressions(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('primary','pool')),
        raw_version TEXT NOT NULL CHECK(typeof(raw_version)='text'),
        policy_version TEXT REFERENCES recommendation_policies(version),
        resolution_reason TEXT CHECK(resolution_reason IN ('missing_policy','invalid_type')),
        historical_version TEXT REFERENCES {HISTORY}(version),
        PRIMARY KEY(impression_id,kind),
        CHECK((policy_version IS NOT NULL)+(historical_version IS NOT NULL)+(resolution_reason IS NOT NULL)=1),
        CHECK(policy_version=raw_version),CHECK(historical_version=raw_version)) WITHOUT ROWID''',
        expressions={'historical_version': 'NULL'})
    conn.execute(f'INSERT INTO {LINKS}(impression_id,kind,raw_version,historical_version) '
                 f'SELECT impression_id,kind,policy_version,policy_version FROM {old}')
    conn.execute(f'DROP TABLE {old}')
    for name, sql in affected.items():
        if name not in {IMMUTABLE, RETAINED, 'historical_impression_capture'}:
            conn.execute(sql)
    conn.execute(f'CREATE INDEX ix_recommendation_impression_historical_policy ON {LINKS}(historical_version) '
                 'WHERE historical_version IS NOT NULL')
    conn.execute(f'''CREATE TRIGGER {RETAINED} BEFORE DELETE ON {LINKS}
        WHEN EXISTS(SELECT 1 FROM rec_served_impressions WHERE id=OLD.impression_id)
        BEGIN SELECT RAISE(ABORT,'Retained impression requires its policy evidence'); END''')
    conn.execute(f'''CREATE TRIGGER {IMMUTABLE} BEFORE UPDATE ON {LINKS}
        WHEN NEW.impression_id IS NOT OLD.impression_id OR NEW.kind IS NOT OLD.kind
        OR NEW.raw_version IS NOT OLD.raw_version
        OR (OLD.historical_version IS NOT NULL AND NEW.historical_version IS NOT OLD.historical_version)
        OR (OLD.policy_version IS NOT NULL AND NEW.policy_version IS NOT OLD.policy_version
            AND NEW.historical_version IS NOT OLD.policy_version)
        BEGIN SELECT RAISE(ABORT,'Impression policy ownership is immutable'); END''')
    capture = capture.replace('policy_version,resolution_reason)', 'policy_version,historical_version,resolution_reason)')
    capture = capture.replace('s.raw_version,p.version,CASE', 's.raw_version,p.version,h.version,CASE')
    capture = capture.replace("WHEN p.version IS NULL THEN 'missing_policy'",
                              "WHEN p.version IS NULL AND h.version IS NULL THEN 'missing_policy'")
    capture = capture.replace('p.version=s.raw_version;', f'p.version=s.raw_version '
                              f'LEFT JOIN {HISTORY} h ON s.valid=1 AND p.version IS NULL AND h.version=s.raw_version;')
    conn.execute(capture)


def validate(conn):
    if set(definitions(conn, (MARKER, *RETIRED))) != {MARKER}:
        raise MigrationError('Policy archive storage was not consolidated')
