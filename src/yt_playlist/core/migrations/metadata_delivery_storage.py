"""Fold provider scheduling and publication references into their owners."""
from ..schema_contract import MigrationError


NAME = 'fold_metadata_delivery_storage'
MARKER = 'ix_metadata_file_selected'
MAINTENANCE = 'listening_evidence_maintenance'
FILES = (
    ('metadata_file', ('kind', 'owner_key', 'revision_key')),
    ('evidence_file', ('kind', 'record_key')),
)
RETIRED = (
    'enrichment_provider_cooldowns', 'enrichment_provider_jobs',
    'provider_refresh_frontiers', 'provider_attempt_credits', 'provider_state_credits',
    'metadata_file_references', 'evidence_file_references',
    'evidence_file_maintenance', 'metadata_native_delivery_heads',
)
FK_OWNERS = ('enrichment_provider_state', 'enrichment_provider_config', 'rec_musical_sources')


def predecessor_objects(conn):
    owners = (*RETIRED, *FK_OWNERS, 'provider_attempts', 'metadata_file_writes', 'evidence_file_writes')
    marks = ','.join('?' for _ in owners)
    return [sql for (sql,) in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND tbl_name IN (" + marks + ") "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name", owners)]


def installed(conn):
    return conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (MARKER,)).fetchone() is not None


def _provider_config(conn):
    # The configuration table has one row per provider. A cooldown can precede
    # configuration, and is retained even if a refresh frontier is absent.
    conn.execute('''CREATE TABLE _provider_config (
        provider TEXT PRIMARY KEY NOT NULL,
        fingerprint TEXT,
        status TEXT,
        cooldown_failures INTEGER,
        cooldown_retry_at REAL,
        cooldown_error TEXT,
        refresh_cursor INTEGER CHECK(typeof(refresh_cursor)='integer' AND refresh_cursor>=0 OR refresh_cursor IS NULL),
        refresh_cutoff INTEGER CHECK(typeof(refresh_cutoff)='integer' AND refresh_cutoff>=0 OR refresh_cutoff IS NULL),
        refresh_fingerprint TEXT,
        refresh_revision INTEGER CHECK(typeof(refresh_revision)='integer' AND refresh_revision>=1 OR refresh_revision IS NULL),
        CHECK((fingerprint IS NULL)=(status IS NULL)),
        CHECK((cooldown_failures IS NULL)=(cooldown_retry_at IS NULL)
          AND (cooldown_failures IS NULL)=(cooldown_error IS NULL)),
        CHECK((refresh_cursor IS NULL)=(refresh_cutoff IS NULL)
          AND (refresh_cursor IS NULL)=(refresh_fingerprint IS NULL)
          AND (refresh_cursor IS NULL)=(refresh_revision IS NULL))
    )''')
    conn.execute('''INSERT INTO _provider_config
        SELECT p.provider,c.fingerprint,c.status,o.failures,o.retry_at,o.error,
               f.cursor,f.cutoff,f.fingerprint,f.revision
        FROM (SELECT provider FROM enrichment_provider_config
              UNION SELECT provider FROM enrichment_provider_cooldowns) p
        LEFT JOIN enrichment_provider_config c ON c.provider=p.provider
        LEFT JOIN enrichment_provider_cooldowns o ON o.provider=p.provider
        LEFT JOIN provider_refresh_frontiers f ON f.provider=p.provider''')
    for table in ('provider_refresh_frontiers', 'enrichment_provider_cooldowns', 'enrichment_provider_config'):
        conn.execute(f'DROP TABLE {table}')
    conn.execute('ALTER TABLE _provider_config RENAME TO enrichment_provider_config')


def _provider_results(conn):
    conn.execute("ALTER TABLE enrichment_provider_state ADD COLUMN queued INTEGER NOT NULL DEFAULT 0 "
                 "CHECK(queued IN (0,1) AND (status<>'queued' OR queued=1))")
    conn.execute('ALTER TABLE enrichment_provider_state ADD COLUMN credit_delivery_id INTEGER REFERENCES metadata_deliveries(id)')
    conn.execute('ALTER TABLE provider_attempts ADD COLUMN artist_credits INTEGER NOT NULL DEFAULT 0 CHECK(artist_credits IN (0,1))')
    conn.execute('''INSERT INTO enrichment_provider_state(recording_id,provider,cycle,at,status,failures,retry_at,error,queued)
        SELECT recording_id,provider,-1,0,'queued',0,0,'',1 FROM enrichment_provider_jobs WHERE 1
        ON CONFLICT(recording_id,provider) DO UPDATE SET queued=1''')
    conn.execute('''UPDATE enrichment_provider_state AS s SET credit_delivery_id=c.delivery_id
        FROM provider_state_credits c WHERE c.recording_id=s.recording_id AND c.provider=s.provider''')
    conn.execute('UPDATE provider_attempts SET artist_credits=1 WHERE id IN (SELECT attempt_id FROM provider_attempt_credits)')
    for event in ('INSERT', 'UPDATE OF recording_id,provider,credit_delivery_id'):
        operation = event.split()[0].lower()
        conn.execute(f'''CREATE TRIGGER owned_provider_credit_{operation}_state BEFORE {event} ON enrichment_provider_state
            WHEN NEW.credit_delivery_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM metadata_deliveries d
                WHERE d.id=NEW.credit_delivery_id AND d.provider=NEW.provider AND d.input_recording_id=NEW.recording_id)
            BEGIN SELECT RAISE(ABORT,'Provider credits have a different subject or provider'); END''')
    for table in ('enrichment_provider_jobs', 'provider_attempt_credits', 'provider_state_credits'):
        conn.execute(f'DROP TABLE {table}')


def _file_selection(conn):
    for prefix, keys in FILES:
        table = f'{prefix}_writes'
        references = f'{prefix}_references'
        conditions = ' OR '.join(f'w.{key} IS NOT r.{key}' for key in keys)
        if conn.execute(f'''SELECT 1 FROM {references} r LEFT JOIN {table} w ON w.id=r.file_id
                WHERE w.file_status IS NOT 'ready' OR {conditions} LIMIT 1''').fetchone():
            raise MigrationError('Selected file does not match its ready reservation: ' + prefix)
        conn.execute(f'ALTER TABLE {table} ADD COLUMN selected INTEGER NOT NULL DEFAULT 0 '
                     "CHECK(selected IN (0,1) AND (selected=0 OR file_status='ready'))")
        conn.execute(f'UPDATE {table} SET selected=1 WHERE id IN (SELECT file_id FROM {references})')
        conn.execute(f'CREATE UNIQUE INDEX ix_{prefix}_selected ON {table}({",".join(keys)}) WHERE selected=1')
        conn.execute(f'DROP TABLE {references}')
    conn.execute('INSERT INTO settings(key,value) SELECT ?,file_status FROM evidence_file_maintenance', (MAINTENANCE,))
    conn.execute('DROP TABLE evidence_file_maintenance')


def _native_sources(conn):
    conn.execute('ALTER TABLE rec_musical_sources ADD COLUMN accepted_run TEXT')
    conn.execute('ALTER TABLE rec_musical_sources ADD COLUMN accepted_at REAL CHECK((accepted_run IS NULL)=(accepted_at IS NULL))')
    conn.execute('ALTER TABLE rec_musical_sources ADD COLUMN accepted_hash BLOB CHECK(accepted_hash IS NULL OR '
                 "(accepted_run IS NOT NULL AND typeof(accepted_hash)='blob' AND length(accepted_hash)=32))")
    # Only a changed source revision needs to enqueue profile work.
    sql = conn.execute("SELECT sql FROM sqlite_schema WHERE name='musical_source_update'").fetchone()[0]
    conn.execute('DROP TRIGGER musical_source_update')
    conn.execute(sql.replace('AFTER UPDATE ON', 'AFTER UPDATE OF revision ON', 1))
    conn.execute('''UPDATE rec_musical_sources AS s
        SET accepted_run=h.run_id,accepted_at=h.accepted_at,accepted_hash=h.result_hash
        FROM metadata_native_delivery_heads h WHERE h.source_key=s.source_key''')
    conn.execute('DROP TABLE metadata_native_delivery_heads')


def install(conn, context):
    _provider_config(conn)
    _provider_results(conn)
    _file_selection(conn)
    _native_sources(conn)


def validate(conn):
    if not installed(conn) or any(conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (name,)).fetchone()
                                  for name in RETIRED):
        raise MigrationError('Incomplete metadata delivery storage migration')
