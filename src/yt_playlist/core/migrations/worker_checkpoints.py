"""Move evidence maintenance and model scheduling to their existing owners."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'own_evidence_maintenance_and_model_schedule'
EVIDENCE = 'evidence_file_writes'
MODELS = 'model_selection_heads'
OWNERS = (EVIDENCE, MODELS)

EVIDENCE_TABLE = """CREATE TABLE evidence_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    record_key TEXT,
    content_digest TEXT CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER CHECK(byte_size IS NULL OR (typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608)),
    file_status TEXT CHECK(file_status IN ('pending','ready')),
    selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1) AND (selected=0 OR file_status='ready')),
    pending INTEGER CHECK(pending IN (0,1)),
    phase TEXT CHECK(phase IN ('inventory','checkpoint','retention')),
    checkpoint_after TEXT,
    CHECK(
        (id=0 AND kind='maintenance' AND record_key IS NULL AND content_digest IS NULL AND byte_size IS NULL
            AND file_status IS NULL AND selected=0 AND pending IS NOT NULL AND phase IS NOT NULL
            AND checkpoint_after IS NOT NULL)
        OR (id>0 AND kind IN ('opportunity','response','forecast','artifact','report','checkpoint')
            AND record_key IS NOT NULL AND content_digest IS NOT NULL AND byte_size IS NOT NULL
            AND file_status IS NOT NULL AND pending IS NULL AND phase IS NULL AND checkpoint_after IS NULL)
    )
)"""


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name IN (?,?) '
                                        'AND sql IS NOT NULL ORDER BY type,name', OWNERS)]


def install(conn, _context):
    attached = [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name=? '
                                             "AND type<>'table' AND sql IS NOT NULL ORDER BY name", (EVIDENCE,))]
    conn.execute('ALTER TABLE evidence_file_writes RENAME TO previous_evidence_file_writes')
    conn.execute(EVIDENCE_TABLE)
    columns = 'id,kind,record_key,content_digest,byte_size,file_status,selected'
    conn.execute(f'INSERT INTO evidence_file_writes({columns}) SELECT {columns} FROM previous_evidence_file_writes')
    conn.execute("""
        INSERT INTO evidence_file_writes(id,kind,pending,phase,checkpoint_after)
        SELECT 0,'maintenance',
            (SELECT value FROM settings WHERE key='listening_evidence_storage_pending') IS '1',
            coalesce((SELECT value FROM settings WHERE key='listening_evidence_storage_phase'), 'inventory'),
            coalesce((SELECT value FROM settings WHERE key='listening_evidence_checkpoint_after'), '')
    """)
    conn.execute("""UPDATE sqlite_sequence SET seq=max(seq,coalesce(
        (SELECT seq FROM sqlite_sequence WHERE name='previous_evidence_file_writes'),0))
        WHERE name='evidence_file_writes'""")
    conn.execute('DROP TABLE previous_evidence_file_writes')
    for sql in attached:
        conn.execute(sql)

    conn.execute('ALTER TABLE model_selection_heads ADD COLUMN schedule_next_at REAL')
    conn.execute('ALTER TABLE model_selection_heads ADD COLUMN schedule_saved_at REAL '
                 'CHECK((schedule_next_at IS NULL)=(schedule_saved_at IS NULL))')
    conn.execute("""
        INSERT INTO model_selection_heads(singleton,schedule_next_at,schedule_saved_at)
        SELECT 1,json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM settings WHERE key='daily_model_schedule'
        ON CONFLICT(singleton) DO UPDATE SET schedule_next_at=excluded.schedule_next_at,
                                           schedule_saved_at=excluded.schedule_saved_at
    """)
    conn.execute("""DELETE FROM settings WHERE key IN (
        'daily_model_schedule','listening_evidence_storage_pending','listening_evidence_storage_phase',
        'listening_evidence_checkpoint_after','listening_evidence_maintenance'
    )""")


def validate(conn):
    from .held_recommendation_state import WINDOWS

    schema = definitions(conn, OWNERS)
    evidence = EVIDENCE_TABLE.replace('    CHECK(\n', '    ' + WINDOWS + ',\n    CHECK(\n', 1)
    if (tokens(schema.get(EVIDENCE)) not in (tokens(EVIDENCE_TABLE), tokens(evidence))
            or 'schedule_next_at' not in tokens(schema.get(MODELS))):
        raise MigrationError('Incomplete worker checkpoint storage')
