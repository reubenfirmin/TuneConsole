"""Keep retryable choice publication on the originating album feedback row."""
from .. import album_feedback_outbox
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'fold_album_feedback_effects'
MARKER = 'ix_album_feedback_queued_response'
COLUMNS = (
    ("queued_feedback", "INTEGER CHECK(queued_feedback IS NULL OR (queued_feedback>0 AND "
                        "action_id IS NOT NULL AND opportunity_id IS NOT NULL AND response<>'clear'))"),
    ("withdrawal_action_id", "TEXT CHECK(withdrawal_action_id IS NULL OR (length(withdrawal_action_id)>0 AND "
                             "action_id IS NOT NULL AND opportunity_id IS NOT NULL))"),
    ("withdrawal_at", "REAL CHECK((withdrawal_at IS NULL)=(withdrawal_action_id IS NULL) AND "
                      "(withdrawal_at IS NULL OR (typeof(withdrawal_at) IN ('real','integer') "
                      "AND abs(withdrawal_at)<=1.7976931348623157e308)))"),
    ("queued_withdrawal", "INTEGER CHECK(queued_withdrawal IS NULL OR "
                          "(queued_withdrawal>0 AND withdrawal_at IS NOT NULL))"),
)


OUTBOX = 'album_feedback_response_outbox'
SQL = '''CREATE TABLE album_feedback_response_outbox (
    id INTEGER PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE CHECK(length(action_id)>0),
    opportunity_id TEXT NOT NULL CHECK(length(opportunity_id)>0),
    kind TEXT NOT NULL CHECK(kind IN ('feedback','withdraw')),
    observed_at REAL NOT NULL CHECK(typeof(observed_at) IN ('real','integer')
        AND abs(observed_at)<=1.7976931348623157e308),
    option_id TEXT,
    feedback_kind TEXT,
    target_action_id TEXT,
    delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1)
        AND (delivered=0 OR kind='withdraw')),
    CHECK((kind='feedback' AND option_id IS NOT NULL
        AND feedback_kind IS NOT NULL AND feedback_kind IN ('more','known','reject')
        AND target_action_id IS NULL)
        OR (kind='withdraw' AND option_id IS NULL AND feedback_kind IS NULL
            AND target_action_id IS NOT NULL AND length(target_action_id)>0)))'''
INDEX_NAME = 'ix_album_feedback_response_pending'
INDEX = f'CREATE INDEX {INDEX_NAME} ON {OUTBOX}(id) WHERE delivered=0'


def _available(conn):
    return conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (OUTBOX,)).fetchone() is not None


def install_outbox(conn, _context):
    if not _available(conn):
        conn.execute(SQL)
        conn.execute(INDEX)
    validate_outbox(conn)


def validate_outbox(conn):
    row = conn.execute('SELECT type,sql FROM main.sqlite_schema WHERE name=?', (OUTBOX,)).fetchone()
    if row is None or row[0] != 'table' or tokens(row[1]) != tokens(SQL):
        raise MigrationError('Incompatible album feedback response outbox')
    row = conn.execute('SELECT type,sql FROM main.sqlite_schema WHERE name=?', (INDEX_NAME,)).fetchone()
    if row is None or row[0] != 'index' or tokens(row[1]) != tokens(INDEX):
        raise MigrationError('Incompatible album feedback response outbox index')


def install(conn, _context):
    for column, declaration in COLUMNS:
        conn.execute(f'ALTER TABLE album_feedback ADD COLUMN {column} {declaration}')
    conn.execute("CREATE INDEX ix_album_feedback_action ON album_feedback(action_id) WHERE action_id IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX ix_album_feedback_withdrawal_action ON album_feedback(withdrawal_action_id) "
                 "WHERE withdrawal_action_id IS NOT NULL")
    conn.execute(f"CREATE UNIQUE INDEX {MARKER} ON album_feedback(queued_feedback) WHERE queued_feedback IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX ix_album_feedback_queued_withdrawal ON album_feedback(queued_withdrawal) "
                 "WHERE queued_withdrawal IS NOT NULL")
    for sequence, action, opportunity, kind, at, option, feedback, target, delivered in conn.execute(
            f'SELECT * FROM {OUTBOX} ORDER BY id'):
        album_feedback_outbox.enqueue(conn, action, opportunity, kind, at, option_id=option,
                                      feedback_kind=feedback, target_action_id=target, sequence=sequence)
        if delivered:
            album_feedback_outbox.acknowledge(conn, action)
    conn.execute(f'DROP TABLE {OUTBOX}')


def validate(conn):
    if set(definitions(conn, (OUTBOX, MARKER))) != {MARKER}:
        raise MigrationError('Album feedback effects must be stored on their originating response')
