"""Own the First Sounds checkpoint and scheduled GitHub release checks."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'own_startup_worker_state'
RADIO = 'radio_sessions'
PROVIDERS = 'enrichment_provider_config'
INDEX = 'uq_first_sounds_checkpoint'
SOUNDS_KEY = 'onboard_first_sounds'
RELEASE_KEYS = ('latest_version_checked_at', 'latest_version_check_after', 'latest_version_seen')
RELEASE_COLUMNS = (
    "release_checked_at REAL CHECK(release_checked_at IS NULL OR (provider='github' AND "
    "abs(release_checked_at)<=1.7976931348623157e308))",
    "release_check_after REAL CHECK(release_check_after IS NULL OR (provider='github' AND "
    "abs(release_check_after)<=1.7976931348623157e308))",
    "latest_release TEXT CHECK(latest_release IS NULL OR (provider='github' AND length(latest_release) BETWEEN 1 AND 256))",
)


def predecessor_objects(conn):
    return list(definitions(conn, (RADIO, PROVIDERS)).values())


def install(conn, _context):
    conn.execute(f'''ALTER TABLE {RADIO} ADD COLUMN first_sounds TEXT CHECK(first_sounds IS NULL OR
        (status='reserved' AND launch_ids='[]' AND seeded_recovery IS NULL AND
         json_type(first_sounds) IS 'object' AND json_extract(first_sounds,'$.episode_id') IS id))''')
    conn.execute(f'CREATE UNIQUE INDEX {INDEX} ON {RADIO}((1)) WHERE first_sounds IS NOT NULL')
    conn.execute(f'''INSERT INTO {RADIO}(id,status,first_sounds)
        SELECT json_extract(value,'$.episode_id'),'reserved',value FROM settings
        WHERE key=? AND CASE WHEN json_valid(value) THEN json_type(value,'$.episode_id') IS 'text' ELSE 0 END''',
        (SOUNDS_KEY,))

    for column in RELEASE_COLUMNS:
        conn.execute(f'ALTER TABLE {PROVIDERS} ADD COLUMN {column}')
    saved = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?,?)', RELEASE_KEYS))
    if saved:
        def clock(key):
            try:
                value = float(saved[key])
                return value if abs(value) <= 1.7976931348623157e308 else None
            except (KeyError, TypeError, ValueError):
                return None

        checked, deadline, latest = RELEASE_KEYS
        conn.execute(f'''INSERT INTO {PROVIDERS}(provider,release_checked_at,release_check_after,latest_release)
            VALUES ('github',?,?,?) ON CONFLICT(provider) DO UPDATE SET
            release_checked_at=excluded.release_checked_at,release_check_after=excluded.release_check_after,
            latest_release=excluded.latest_release''', (clock(checked), clock(deadline), saved.get(latest) or None))
    conn.execute('DELETE FROM settings WHERE key IN (?,?,?,?)', (SOUNDS_KEY, *RELEASE_KEYS))


def validate(conn):
    layout = definitions(conn, (RADIO, PROVIDERS, INDEX))
    if (INDEX not in layout or 'first_sounds' not in tokens(layout.get(RADIO))
            or not all(column.split()[0] in tokens(layout.get(PROVIDERS)) for column in RELEASE_COLUMNS)):
        raise MigrationError('Invalid startup worker state')
