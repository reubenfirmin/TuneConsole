"""Atomic response intents and compact receipts for once-only album withdrawals."""
from contextlib import contextmanager
import time

from . import policy_documents
from .activity import WorkBudgetExceeded, check_cancellable_work
from .evidence_ledger import _mkdir, root_for_database


TABLE = 'album_feedback_response_outbox'
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
INDEX = f'CREATE INDEX {INDEX_NAME} ON {TABLE}(id) WHERE delivered=0'


def available(conn):
    return conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (TABLE,)).fetchone() is not None


def install(conn, context=None):
    if not available(conn):
        conn.execute(SQL)
        conn.execute(INDEX)
    validate(conn)


def validate(conn):
    from .schema_contract import MigrationError, tokens
    row = conn.execute('SELECT type,sql FROM main.sqlite_schema WHERE name=?', (TABLE,)).fetchone()
    if row is None or row[0] != 'table' or tokens(row[1]) != tokens(SQL):
        raise MigrationError('Incompatible album feedback response outbox')
    row = conn.execute('SELECT type,sql FROM main.sqlite_schema WHERE name=?', (INDEX_NAME,)).fetchone()
    if row is None or row[0] != 'index' or tokens(row[1]) != tokens(INDEX):
        raise MigrationError('Incompatible album feedback response outbox index')


def enqueue(conn, action_id, opportunity_id, kind, now, *, option_id=None,
            feedback_kind=None, target_action_id=None):
    """The judgment and every resulting intent belong to the caller's SQL unit."""
    if not conn.in_transaction:
        raise RuntimeError('Album feedback capture requires its judgment transaction')
    values = (action_id, opportunity_id, kind, now, option_id, feedback_kind, target_action_id)
    conn.execute(f'INSERT INTO {TABLE}(action_id,opportunity_id,kind,observed_at,option_id,'
        'feedback_kind,target_action_id) VALUES (?,?,?,?,?,?,?) ON CONFLICT(action_id) DO NOTHING', values)
    retained = conn.execute(f'SELECT action_id,opportunity_id,kind,observed_at,option_id,'
        f'feedback_kind,target_action_id FROM {TABLE} WHERE action_id=?', (action_id,)).fetchone()
    # A withdrawal has one identity for the original action. Replacing or
    # clearing again must retain its first observation, even before delivery.
    compared = tuple(retained)
    if kind == 'withdraw':
        compared = (*compared[:3], now, *compared[4:])
    if compared != values:
        raise ValueError('Album feedback response ID was reused')


def pending(conn, *, after=0, limit=32):
    if not available(conn):
        return []
    return conn.execute(f'SELECT * FROM {TABLE} WHERE delivered=0 AND id>? ORDER BY id LIMIT ?', (after, limit)).fetchall()


def acknowledge(conn, action_id):
    # Native history can return the same original actions on later clears or
    # alias merges. Their small typed receipt avoids re-enqueuing every already
    # delivered withdrawal; completed feedback needs no such retained receipt.
    conn.execute(f'DELETE FROM {TABLE} WHERE action_id=? AND kind=\'feedback\'', (action_id,))
    conn.execute(f'UPDATE {TABLE} SET delivered=1 WHERE action_id=? AND kind=\'withdraw\'', (action_id,))


@contextmanager
def fence(store, *, wait_seconds=1):
    """Exclude final evidence retirement while capturing, without waiting under ServingLock.

    This independent OS fence permits capture while the evidence publisher is
    busy. The collector takes it only for its final pin check and deletion.
    """
    directory = root_for_database(store._db_path, store._evidence_key)
    _mkdir(directory)
    deadline = time.monotonic() + wait_seconds
    with (directory / '.album-feedback.lock').open('a+b') as stream:
        while True:
            check_cancellable_work()
            unlock = policy_documents._try_file_lock(stream, exclusive=True)
            if unlock is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or store._lock.held_by_current_thread:
                raise WorkBudgetExceeded('Album feedback capture is busy')
            time.sleep(min(.01, remaining))
        try:
            yield
        finally:
            unlock()
