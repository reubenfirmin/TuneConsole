"""Retire old artist captures while retaining exact delivery identities.

Heads and selections remain active. Retired deliveries retain their credit,
with no ownership of the metadata delivery or current selection.
"""
import json

BATCH = 128
RETENTION_SECONDS = 30 * 86400
CHECKPOINT = 'artist_credit_retention'


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


def install(conn, context=None):
    for sql in statements():
        conn.execute(sql)
    conn.execute('INSERT INTO artist_credit_retention(id) VALUES (1)')


def validate(conn):
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    from .schema_inspection import definitions
    expected = {sql.split()[2]: sql for sql in statements()}
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or _tokens(actual[name]) != _tokens(sql):
            raise MigrationError('Incompatible artist credit retention owner: ' + name)
    if not conn.execute('SELECT 1 FROM artist_credit_retention WHERE id=1').fetchone():
        raise MigrationError('Missing artist credit retention state')


def state(conn):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (CHECKPOINT,)).fetchone()
    return json.loads(row[0]) if row else {'enabled': 0, 'cursor': 0, 'completed': 0, 'next_scan_at': 0}


def step(conn, now, *, activate=False):
    """Inspect at most BATCH active captures and commit retirement with the cursor."""
    if not conn.in_transaction:
        raise RuntimeError('Artist capture retirement requires an outer transaction')
    if type(now) not in (int, float) or not -1.7976931348623157e308 <= now <= 1.7976931348623157e308:
        raise ValueError('Artist credit capture time must be finite')
    checkpoint = state(conn)
    if not checkpoint['enabled']:
        if not activate:
            return False
        checkpoint['enabled'] = 1
    if not activate and now < checkpoint['next_scan_at']:
        return False
    rows = conn.execute('SELECT o.* FROM artist_credit_observations o WHERE retired=0 AND o.id>? ORDER BY o.id LIMIT ?',
                        (checkpoint['cursor'], BATCH)).fetchall()
    for row in rows:
        # Unknown-time conversion duplicates have no independent runtime use.
        # Reconciliation and unknown-time fresh evidence remain conservative.
        obsolete = row['capture_kind'] in ('legacy_capture', 'selection_snapshot')
        expired = (row['capture_kind'] in ('capture', 'provider_delivery') and row['last_seen'] is not None
                   and row['last_seen'] < now - RETENTION_SECONDS)
        if obsolete or expired:
            if row['delivery_key'] is None:
                conn.execute('DELETE FROM artist_credit_observations WHERE id=? '
                             'AND is_capture_head=0 AND is_selected=0', (row['id'],))
            else:
                conn.execute('UPDATE artist_credit_observations SET retired=1,metadata_delivery_id=NULL '
                             'WHERE id=? AND is_capture_head=0 AND is_selected=0', (row['id'],))
    if rows:
        checkpoint.update(cursor=rows[-1]['id'], next_scan_at=0)
    else:
        checkpoint.update(cursor=0, completed=1, next_scan_at=now + 86400)
    conn.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(checkpoint), CHECKPOINT))
    return bool(rows)
