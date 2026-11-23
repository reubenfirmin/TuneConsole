"""Keep album controls, maintenance checkpoints and model roles with their owners."""
import json

from .. import album_tuning_state, credit_retention, metadata_contract, mode_retention, model_selection_heads
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from .recommendation_candidate_storage import SETTINGS_GUARDS


NAME = 'fold_settings_and_model_selection_controls'
RETIRED = ('album_menu_controls', 'metadata_runtime_state', 'taste_mode_retention',
           'model_selection_heads', 'taste_mode_retired_payloads')
RUNS = 'rec_model_selection_runs'
ROLE_COLUMNS = tuple(zip(model_selection_heads.KEYS.values(),
                         ('active_run_id', 'display_run_id', 'restore_run_id'), strict=True))
ALLOCATION_TRIGGERS = ('monotonic_taste_mode_allocation', 'allocated_taste_mode_insert',
                       'allocated_taste_mode_delete', 'allocated_taste_mode_update',
                       'complete_taste_mode_reactivation', 'published_taste_mode_reactivation')


def predecessor_metadata_active(conn):
    return bool(conn.execute('SELECT active FROM metadata_runtime_state WHERE id=1').fetchone()[0])


def predecessor_objects(conn):
    owners = (*RETIRED, 'taste_modes', RUNS, *SETTINGS_GUARDS, *ALLOCATION_TRIGGERS)
    placeholders = ','.join('?' for _ in owners)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM main.sqlite_schema WHERE sql IS NOT NULL AND (name IN ({placeholders}) '
        "OR (type='trigger' AND instr(sql,?)>0)) ORDER BY name",
        (*owners, metadata_contract.PREDECESSOR_ACTIVE_SQL))]


def _replace_metadata_triggers(conn):
    previous = metadata_contract.PREDECESSOR_ACTIVE_SQL
    triggers = conn.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND instr(sql,?) ORDER BY name",
                            (previous,)).fetchall()
    for name, sql in triggers:
        conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(sql.replace(previous, metadata_contract.ACTIVE_SQL))


def _mode_triggers(conn):
    high_water = mode_retention.HIGH_WATER
    conn.execute(f"""CREATE TRIGGER monotonic_taste_mode_allocation BEFORE UPDATE OF value ON settings
        WHEN OLD.key='{high_water}' AND CAST(NEW.value AS INTEGER)<CAST(OLD.value AS INTEGER)
        BEGIN SELECT RAISE(ABORT,'Mode allocation cannot go backwards'); END""")
    for event, identities in (('INSERT', 'NEW.mode_id'), ('DELETE', 'OLD.mode_id'),
                              ('UPDATE OF mode_id', 'OLD.mode_id,NEW.mode_id')):
        name = 'allocated_taste_mode_' + event.split()[0].lower()
        conn.execute(f"""CREATE TRIGGER {name} BEFORE {event} ON taste_modes
            BEGIN UPDATE settings SET value=max(CAST(value AS INTEGER),{identities})
            WHERE key='{high_water}' AND max(CAST(value AS INTEGER),{identities})>CAST(value AS INTEGER); END""")
    conn.execute("""CREATE TRIGGER complete_taste_mode_reactivation BEFORE UPDATE ON taste_modes
        WHEN NEW.active=1 AND length(NEW.centroid)=0 AND OLD.payload_retired_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'Retired mode reactivation requires a new payload'); END""")
    conn.execute("""CREATE TRIGGER published_taste_mode_reactivation AFTER UPDATE ON taste_modes
        WHEN NEW.active=1 AND NEW.payload_retired_at IS NOT NULL
        BEGIN UPDATE taste_modes SET payload_retired_at=NULL WHERE mode_id=NEW.mode_id; END""")


def _save_settings(conn, state):
    conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)',
                     [(key, json.dumps(value)) for key, value in state.items()])


def install(conn, _context):
    for name, sql in definitions(conn, SETTINGS_GUARDS).items():
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(sql.replace(f"'{album_tuning_state.TUNING}',", ''))
    album = conn.execute('''SELECT expires_at,novelty,genres_json,refresh_epoch,refresh_exclusions_json,refresh_updated_at
                            FROM album_menu_controls''').fetchone()
    state = {}
    if album is not None:
        expires, novelty, genres, epoch, exclusions, refreshed = album
        if expires is not None:
            state[album_tuning_state.TUNING] = {'until': expires, 'values': {'novelty': novelty}, 'genres': json.loads(genres)}
        if exclusions is not None:
            refresh = {'updated_at': refreshed}
            if epoch is not None:
                refresh.update(epoch=epoch, exclude=json.loads(exclusions))
            state[album_tuning_state.REFRESH] = refresh
    active, cursor, after, shard, caught = conn.execute('''SELECT active,gc_cursor,gc_after,gc_shard,enrichment_caught_up_at
                                                          FROM metadata_runtime_state''').fetchone()
    state[metadata_contract.ACTIVE] = active
    state[metadata_contract.EXPIRY] = {'gc_cursor': cursor, 'gc_after': after, 'gc_shard': shard}
    if caught is not None:
        state[metadata_contract.CAUGHT_UP] = caught
    _replace_metadata_triggers(conn)

    enabled, cursor, after, high_water, credit_enabled, credit_cursor, completed, credit_after = conn.execute('''
        SELECT enabled,cursor,next_scan_at,high_water,credit_enabled,credit_cursor,credit_completed,credit_next_scan_at
        FROM taste_mode_retention''').fetchone()
    state[mode_retention.HIGH_WATER] = high_water
    state[mode_retention.CHECKPOINT] = {'enabled': enabled, 'cursor': cursor, 'next_scan_at': after}
    state[credit_retention.CHECKPOINT] = {'enabled': credit_enabled, 'cursor': credit_cursor,
                                         'completed': completed, 'next_scan_at': credit_after}
    for name in ALLOCATION_TRIGGERS:
        conn.execute(f'DROP TRIGGER {name}')
    conn.execute('ALTER TABLE taste_modes ADD COLUMN payload_retired_at REAL')
    conn.execute('''UPDATE taste_modes SET payload_retired_at=(
        SELECT retired_at FROM taste_mode_retired_payloads r WHERE r.mode_id=taste_modes.mode_id)
        WHERE mode_id IN (SELECT mode_id FROM taste_mode_retired_payloads)''')
    _mode_triggers(conn)

    for role, previous in ROLE_COLUMNS:
        conn.execute(f'ALTER TABLE {RUNS} ADD COLUMN {role} INTEGER NOT NULL DEFAULT 0 CHECK({role} IN (0,1))')
        conn.execute(f'UPDATE {RUNS} SET {role}=1 WHERE id=(SELECT {previous} FROM model_selection_heads)')
        conn.execute(f'CREATE UNIQUE INDEX ix_{RUNS}_{role} ON {RUNS}({role}) WHERE {role}=1')
    schedule = conn.execute('SELECT schedule_next_at,schedule_saved_at FROM model_selection_heads '
                            'WHERE schedule_next_at IS NOT NULL').fetchone()
    if schedule is not None:
        state['daily_model_schedule'] = {'next_at': schedule[0], 'saved_at': schedule[1]}
    _save_settings(conn, state)

    for name in RETIRED:
        conn.execute(f'DROP TABLE {name}')


def validate(conn, *, modes_folded=False):
    mode_owner, retired_column = ('taste_groups', 'mode_payload_retired_at') if modes_folded else (
        'taste_modes', 'payload_retired_at')
    schema = definitions(conn, (*RETIRED, mode_owner, RUNS))
    if (set(RETIRED).intersection(schema) or retired_column not in tokens(schema.get(mode_owner))
            or not set(model_selection_heads.KEYS.values()) <= set(tokens(schema.get(RUNS)))):
        raise MigrationError('Incomplete control settings storage')
    for key in (metadata_contract.ACTIVE, metadata_contract.EXPIRY, mode_retention.HIGH_WATER,
                mode_retention.CHECKPOINT, credit_retention.CHECKPOINT):
        if conn.execute('SELECT 1 FROM settings WHERE key=? AND json_valid(value)', (key,)).fetchone() is None:
            raise MigrationError('Missing control setting '+key)
