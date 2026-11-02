"""Retired mode identity summaries, without obsolete vectors or track lists."""

BATCH = 128


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


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='taste_mode_retention'").fetchone() is not None


def install(conn, context=None):
    for sql in statements():
        conn.execute(sql)
    # MAX uses the INTEGER PRIMARY KEY, not a history scan or conversion.
    conn.execute('INSERT INTO taste_mode_retention(id,high_water) SELECT 1,coalesce(max(mode_id),0) FROM taste_modes')


def validate(conn):
    from .schema_contract import tokens as _tokens
    from .schema_contract import MigrationError
    for sql in statements():
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible taste mode retirement owner: ' + name)
    if not conn.execute('SELECT 1 FROM taste_mode_retention WHERE id=1').fetchone():
        raise MigrationError('Missing taste mode retirement state')


def step(conn, now, *, activate=False):
    if not conn.in_transaction:
        raise RuntimeError('Mode retirement requires an outer transaction')
    if not available(conn):
        return False
    from .artist_credits import _clock
    _clock(now)
    enabled, cursor, next_scan = conn.execute('SELECT enabled,cursor,next_scan_at FROM taste_mode_retention WHERE id=1').fetchone()
    if not enabled:
        if not activate:
            return False
        conn.execute('UPDATE taste_mode_retention SET enabled=1 WHERE id=1')
    if not activate and now < next_scan:
        return False
    rows = conn.execute('SELECT mode_id FROM taste_modes WHERE mode_id>? ORDER BY mode_id LIMIT ?',
                        (cursor, BATCH)).fetchall()
    for (identity,) in rows:
        # Keep the identity row for all historical FKs. The writer reservation
        # fences reactivation; existing SQL snapshots keep their old payload.
        if conn.execute('SELECT 1 FROM taste_modes WHERE mode_id=? AND active=0', (identity,)).fetchone():
            conn.execute('INSERT INTO taste_mode_retired_payloads VALUES (?,?) ON CONFLICT DO NOTHING', (identity, now))
            conn.execute("UPDATE taste_modes SET centroid=X'',families='[]',rep_keys='[]' WHERE mode_id=? "
                         "AND active=0 AND (length(centroid)>0 OR families<>'[]' OR rep_keys<>'[]')", (identity,))
    conn.execute('UPDATE taste_mode_retention SET cursor=?,next_scan_at=? WHERE id=1',
                 (rows[-1][0] if rows else 0, now + 86400 if not rows else 0))
    return bool(rows)
