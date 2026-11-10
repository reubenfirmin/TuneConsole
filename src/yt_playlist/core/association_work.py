"""SQLite checkpoints for independently committed recording-association steps."""

SQL = '''CREATE TABLE recording_association_work (
    video_id TEXT PRIMARY KEY REFERENCES youtube_items(video_id),
    association_id INTEGER NOT NULL UNIQUE REFERENCES item_recording_associations(id),
    plan_json TEXT NOT NULL,
    assigned INTEGER NOT NULL DEFAULT 0 CHECK(assigned IN (0,1)),
    discovery INTEGER NOT NULL DEFAULT 0 CHECK(discovery IN (0,1)),
    keys INTEGER NOT NULL DEFAULT 0 CHECK(keys IN (0,1)),
    redirected INTEGER NOT NULL DEFAULT 0 CHECK(redirected IN (0,1)),
    model INTEGER NOT NULL DEFAULT 0 CHECK(model IN (0,1)))'''


def install(conn, context=None):
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recording_association_work'").fetchone():
        conn.execute(SQL)
    validate(conn)


def validate(conn):
    from .schema_contract import MigrationError, tokens
    row = conn.execute("SELECT type,sql FROM main.sqlite_schema WHERE name='recording_association_work'").fetchone()
    if row is None or row[0] != 'table' or tokens(row[1]) != tokens(SQL):
        raise MigrationError('Incompatible recording association checkpoints')


def complete(conn, association, step):
    if association is None:
        return
    if step not in ('assigned', 'discovery', 'keys', 'redirected', 'model') or not conn.in_transaction:
        raise ValueError('Association step requires its owning write unit')
    conn.execute(f'UPDATE recording_association_work SET {step}=1 WHERE association_id=?', (association,))
