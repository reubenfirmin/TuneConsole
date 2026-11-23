"""SQLite checkpoints for independently committed recording-association steps."""

STEPS = ('assigned', 'discovery', 'keys', 'redirected', 'model')
MARKER = 'ix_recording_association_unfinished'
SERIAL = 'recording_association_serial'
PENDING_ID = "association_work->>'id'"
PENDING = f'association_work IS NOT NULL AND {PENDING_ID}=?'

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
    if step not in STEPS or not conn.in_transaction:
        raise ValueError('Association step requires its owning write unit')
    conn.execute(f"UPDATE track_sources SET association_work=json_set(association_work,'$.{step}',1) WHERE {PENDING}",
                 (association,))


def append(conn, video, recording, previous, source, evidence, at):
    if not conn.in_transaction:
        raise RuntimeError('Recording association history requires an outer transaction')
    identity = conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key=? RETURNING value",
                            (SERIAL,)).fetchone()[0]
    changed = conn.execute("UPDATE track_sources SET association_history=json_insert(association_history,'$[#]',"
                           "json_array(:id,:recording,:previous,:source,:evidence,"
                           "json(CASE WHEN :at IS NOT NULL THEN printf('%!.26g',:at) END))) WHERE video_id=:video",
                           dict(id=int(identity), recording=recording, previous=previous, source=source,
                                evidence=evidence, at=at, video=video)).rowcount
    if changed != 1:
        raise ValueError('Unknown recording association upload')
    return int(identity)


def admit(conn, video, recording, at):
    if conn.execute("SELECT 1 FROM track_sources WHERE video_id=? AND association_history='[]'", (video,)).fetchone():
        append(conn, video, recording, None, 'new_upload', '{}', at)


def pending(conn, video):
    columns = ','.join(f"COALESCE(association_work->>'{step}',0) AS {step}" for step in STEPS)
    row = conn.execute(f"SELECT association_work->>'plan_json' AS plan_json,{columns} FROM track_sources "
                       'WHERE video_id=? AND association_work IS NOT NULL', (video,)).fetchone()
    return dict(row) if row else None


def save(conn, video, identity, plan):
    changed = conn.execute("UPDATE track_sources SET association_work=json_object('id',?,'plan_json',?) "
        'WHERE video_id=? AND association_work IS NULL AND EXISTS('
        'SELECT 1 FROM json_each(association_history) h WHERE h.value->>0=?)',
        (identity, plan, video, identity)).rowcount
    if changed != 1:
        raise ValueError('Recording correction already has a plan or belongs to another upload')


def finish(conn, identity):
    conn.execute(f'UPDATE track_sources SET association_work=NULL WHERE {PENDING}', (identity,))
