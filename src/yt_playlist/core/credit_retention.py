"""Compact delivery receipts and explicitly enabled artist-capture retirement.

Only heads/selections are live observation owners. Immutable ordered content is
retained independently; a replay receipt must never reapply an old selection.
"""
import sqlite3

BATCH = 128
RETENTION_SECONDS = 30 * 86400


def statements():
    yield ("CREATE TABLE artist_credit_replays (entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),"
           "source TEXT NOT NULL CHECK(length(source)>0),delivery_key TEXT NOT NULL CHECK(length(delivery_key)>0),"
           "observation_id INTEGER NOT NULL UNIQUE,credit_id INTEGER NOT NULL REFERENCES artist_credits(id),"
           "metadata_bound INTEGER NOT NULL CHECK(metadata_bound IN (0,1)),"
           "PRIMARY KEY(entry_id,source,delivery_key)) WITHOUT ROWID")
    yield ("CREATE TRIGGER immutable_artist_credit_replay BEFORE UPDATE ON artist_credit_replays "
           "BEGIN SELECT RAISE(ABORT,'Artist delivery replay identity is immutable'); END")
    yield ('CREATE TRIGGER retained_replay_credit_member BEFORE DELETE ON artist_credit_members '
           'WHEN EXISTS(SELECT 1 FROM artist_credit_replays WHERE credit_id=OLD.credit_id) '
           "BEGIN SELECT RAISE(ABORT,'Replay credit content is immutable'); END")
    yield ("CREATE TABLE artist_credit_retention (id INTEGER PRIMARY KEY CHECK(id=1),"
           "enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),"
           "cursor INTEGER NOT NULL DEFAULT 0,completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)),"
           "next_scan_at REAL NOT NULL DEFAULT 0)")


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='artist_credit_replays'").fetchone() is not None


def install(conn, context=None):
    for sql in statements():
        conn.execute(sql)
    conn.execute('INSERT INTO artist_credit_retention(id) VALUES (1)')


def validate(conn):
    from .migrations.current import _tokens
    from .migrations.runner import MigrationError
    for sql in statements():
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible artist credit retention owner: ' + name)
    if not conn.execute('SELECT 1 FROM artist_credit_retention WHERE id=1').fetchone():
        raise MigrationError('Missing artist credit retention state')


def receipt(conn, entry, source, key, observation, credit, *, metadata_bound=False):
    """Publish in the capture's transaction; retries cannot replace an outcome."""
    if not conn.in_transaction:
        raise RuntimeError('Artist replay receipts require an outer transaction')
    old = conn.execute('SELECT observation_id,credit_id,metadata_bound FROM artist_credit_replays '
                       'WHERE entry_id=? AND source=? AND delivery_key=?', (entry, source, key)).fetchone()
    if old is not None and tuple(old) != (observation, credit, int(metadata_bound)):
        raise sqlite3.IntegrityError('Artist delivery replay outcome changed')
    if old is None:
        conn.execute('INSERT INTO artist_credit_replays VALUES (?,?,?,?,?,?)',
                     (entry, source, key, observation, credit, int(metadata_bound)))


def prepare_receipt(conn, observation):
    """Adopt a capture without erasing a previously verified custody binding.

    Compact metadata can release the nullable delivery FK after a bound replay
    receipt has been published. A later retention scan must keep that outcome.
    """
    key = observation['delivery_key']
    if key is None:
        return
    bound = observation['metadata_delivery_id'] is not None
    if not bound:
        old = conn.execute('SELECT metadata_bound FROM artist_credit_replays WHERE entry_id=? AND source=? '
                           'AND delivery_key=?', (observation['entry_id'], observation['source'], key)).fetchone()
        bound = old is not None and bool(old[0])
    receipt(conn, observation['entry_id'], observation['source'], key, observation['id'],
            observation['credit_id'], metadata_bound=bound)


def _owners(conn):
    """Refuse destructive preparation if a later owner has not been audited."""
    owners = set()
    for (name,) in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
        owners.update(name for row in conn.execute('PRAGMA foreign_key_list("' + name.replace('"', '""') + '")')
                      if row[2] == 'artist_credit_observations')
    if owners != {'artist_credit_capture_heads', 'entry_artist_selections'}:
        raise ValueError('Artist observation owners require a new retention audit: ' + repr(owners))


def step(conn, now, *, activate=False):
    """Inspect at most BATCH IDs; commit cursor, receipts and retirement together."""
    if not conn.in_transaction:
        raise RuntimeError('Artist capture retirement requires an outer transaction')
    if not available(conn):
        return False
    if type(now) not in (int, float) or not -1.7976931348623157e308 <= now <= 1.7976931348623157e308:
        raise ValueError('Artist credit capture time must be finite')
    state = conn.execute('SELECT enabled,cursor,next_scan_at FROM artist_credit_retention WHERE id=1').fetchone()
    if not state[0]:
        if not activate:
            return False
        _owners(conn)
        conn.execute('UPDATE artist_credit_retention SET enabled=1 WHERE id=1')
    if not activate and now < state[2]:
        return False
    rows = conn.execute('SELECT o.* FROM artist_credit_observations o WHERE o.id>? ORDER BY o.id LIMIT ?',
                        (state[1], BATCH)).fetchall()
    for row in rows:
        prepare_receipt(conn, row)
        # Unknown-time conversion duplicates have no independent runtime use.
        # Reconciliation and unknown-time fresh evidence remain conservative.
        obsolete = row['capture_kind'] in ('legacy_capture', 'selection_snapshot')
        expired = (row['capture_kind'] in ('capture', 'provider_delivery') and row['last_seen'] is not None
                   and row['last_seen'] < now - RETENTION_SECONDS)
        if obsolete or expired:
            conn.execute('DELETE FROM artist_credit_observations WHERE id=? '
                'AND NOT EXISTS(SELECT 1 FROM artist_credit_capture_heads WHERE observation_id=?) '
                'AND NOT EXISTS(SELECT 1 FROM entry_artist_selections WHERE observation_id=?)',
                (row['id'], row['id'], row['id']))
    cursor = rows[-1]['id'] if rows else 0
    conn.execute('UPDATE artist_credit_retention SET cursor=?,completed=CASE WHEN ?=0 THEN 1 ELSE completed END,'
                 'next_scan_at=? WHERE id=1', (cursor, cursor, now + 86400 if not rows else 0))
    return bool(rows)
