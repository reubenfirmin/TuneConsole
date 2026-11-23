"""Retired mode identity summaries, without obsolete vectors or track lists."""
import json

BATCH = 128
CHECKPOINT = 'taste_mode_maintenance'
HIGH_WATER = 'taste_mode_high_water'
CREDIT_COLUMNS = (
    'credit_enabled INTEGER NOT NULL DEFAULT 0 CHECK(credit_enabled IN (0,1))',
    'credit_cursor INTEGER NOT NULL DEFAULT 0',
    'credit_completed INTEGER NOT NULL DEFAULT 0 CHECK(credit_completed IN (0,1))',
    'credit_next_scan_at REAL NOT NULL DEFAULT 0',
)


def statements():
    yield ("CREATE TABLE taste_mode_retired_payloads (mode_id INTEGER PRIMARY KEY REFERENCES taste_modes(mode_id),"
           "retired_at REAL NOT NULL)")
    yield ("CREATE TABLE taste_mode_retention (id INTEGER PRIMARY KEY CHECK(id=1),"
           "enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),cursor INTEGER NOT NULL DEFAULT 0,"
           "high_water INTEGER NOT NULL DEFAULT 0,next_scan_at REAL NOT NULL DEFAULT 0)")
    yield ("CREATE TRIGGER monotonic_taste_mode_allocation BEFORE UPDATE OF high_water ON taste_mode_retention "
           "WHEN NEW.high_water<OLD.high_water BEGIN SELECT RAISE(ABORT,'Mode allocation cannot go backwards'); END")
    for operation, side in (('INSERT', 'NEW'), ('DELETE', 'OLD')):
        yield (f'CREATE TRIGGER allocated_taste_mode_{operation.lower()} BEFORE {operation} ON taste_modes '
               f'BEGIN UPDATE taste_mode_retention SET high_water=max(high_water,{side}.mode_id) WHERE id=1; END')
    yield ('CREATE TRIGGER allocated_taste_mode_update BEFORE UPDATE OF mode_id ON taste_modes '
           'BEGIN UPDATE taste_mode_retention SET high_water=max(high_water,OLD.mode_id,NEW.mode_id) WHERE id=1; END')
    yield ('CREATE TRIGGER complete_taste_mode_reactivation BEFORE UPDATE ON taste_modes WHEN NEW.active=1 '
           'AND length(NEW.centroid)=0 AND EXISTS(SELECT 1 FROM taste_mode_retired_payloads WHERE mode_id=OLD.mode_id) '
           "BEGIN SELECT RAISE(ABORT,'Retired mode reactivation requires a new payload'); END")
    yield ('CREATE TRIGGER published_taste_mode_reactivation AFTER UPDATE ON taste_modes WHEN NEW.active=1 '
           'BEGIN DELETE FROM taste_mode_retired_payloads WHERE mode_id=OLD.mode_id; END')


def install(conn, context=None):
    for sql in statements():
        conn.execute(sql)
    # MAX uses the INTEGER PRIMARY KEY, not a history scan or conversion.
    conn.execute('INSERT INTO taste_mode_retention(id,high_water) SELECT 1,coalesce(max(mode_id),0) FROM taste_modes')


def validate(conn):
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    from .schema_inspection import definitions
    expected = {sql.split()[2]: sql for sql in statements()}
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        layouts = [sql]
        if name == 'taste_mode_retention':
            layouts.append(sql[:-1] + ',' + ','.join(CREDIT_COLUMNS) + ')')
        if _tokens(actual.get(name)) not in tuple(map(_tokens, layouts)):
            raise MigrationError('Incompatible taste mode retirement owner: ' + name)
    if not conn.execute('SELECT 1 FROM taste_mode_retention WHERE id=1').fetchone():
        raise MigrationError('Missing taste mode retirement state')


def step(conn, now, *, activate=False):
    if not conn.in_transaction:
        raise RuntimeError('Mode retirement requires an outer transaction')
    from .artist_credits import _clock
    _clock(now)
    row = conn.execute('SELECT value FROM settings WHERE key=?', (CHECKPOINT,)).fetchone()
    checkpoint = json.loads(row[0])
    if not activate and (not checkpoint['enabled'] or now < checkpoint['next_scan_at']):
        return False
    rows = conn.execute('SELECT legacy_mode_id FROM taste_groups WHERE legacy_mode_id>? AND mode_label IS NOT NULL '
                        'ORDER BY legacy_mode_id LIMIT ?',
                        (checkpoint['cursor'], BATCH)).fetchall()
    conn.executemany("UPDATE taste_groups SET mode_centroid=X'',mode_families='[]',mode_rep_keys='[]',"
                     "mode_payload_retired_at=coalesce(mode_payload_retired_at,?) WHERE legacy_mode_id=? AND mode_active=0 "
                     "AND (mode_payload_retired_at IS NULL OR length(mode_centroid)>0 OR mode_families<>'[]' OR mode_rep_keys<>'[]')",
                     [(now, identity) for (identity,) in rows])
    checkpoint.update(enabled=1, cursor=rows[-1][0] if rows else 0,
                      next_scan_at=0 if rows else now + 86400)
    conn.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(checkpoint), CHECKPOINT))
    return bool(rows)
