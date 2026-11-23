"""Move the current Home controls and saved taste snapshots to settings."""
import json

from .. import collection_recovery_epoch, home_offers, home_revisit_progress, recommendation_requests
from .. import taste_control_state, worker_settings
from ..collection_owners import TASTE_CONTROLS
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from ..taste_settings import BASELINE, CHECKPOINT, STATE_KEYS


NAME = 'own_home_and_saved_taste_settings'
RETIRED = ('home_genre_requests', *TASTE_CONTROLS)


def _guards():
    references = {
        'recordings': (
            "EXISTS(SELECT 1 FROM mood_events o,json_each(o.seeds) f WHERE f.value->>'recording_id'=OLD.id) OR "
            f"EXISTS(SELECT 1 FROM settings s,json_each(s.value,'$.entries') f "
            f"WHERE s.key='{STATE_KEYS[BASELINE]}' AND f.value->>'recording_id'=OLD.id)"
        ),
        'taste_axes': (
            f"EXISTS(SELECT 1 FROM settings s,json_each(s.value,'$.weights') f "
            f"WHERE s.key='{STATE_KEYS[CHECKPOINT]}' AND f.value->>'axis_id'=OLD.id)"
        ),
    }
    for parent, retained in references.items():
        for operation, event, changed in (('delete', 'DELETE', ''), ('rekey', 'UPDATE OF id', 'NEW.id IS NOT OLD.id AND ')):
            name = f'mood_taste_retains_{parent}_{operation}'
            yield name, f'''CREATE TRIGGER {name} BEFORE {event} ON {parent}
                WHEN {changed}({retained})
                BEGIN SELECT RAISE(ABORT,'A saved mood or taste snapshot retains this identity'); END'''


def predecessor_objects(conn):
    guard_names = tuple(name for name, _ in _guards())
    return [sql for sql, in conn.execute(
        'SELECT sql FROM sqlite_schema WHERE (tbl_name IN (?,?,?) OR name IN (?,?,?,?)) '
        'AND sql IS NOT NULL ORDER BY type,name', (*RETIRED, *guard_names))]


def _copy_home(conn):
    row = conn.execute('SELECT * FROM home_genre_requests WHERE id=1').fetchone()
    if row is None:
        return
    if row['epoch'] is not None or row['updated_at'] is not None:
        request = {'updated_at': row['updated_at']}
        if row['epoch'] is not None:
            request['epoch'] = row['epoch']
            for field in ('include_genres', 'avoid_genres'):
                if row[field] is not None:
                    request[field] = json.loads(row[field])
            for field in ('held_by_vibe', 'suspended_by_vibe'):
                if row[field] is not None:
                    request[field] = bool(row[field])
        worker_settings.save(conn, recommendation_requests.HOME_GENRE, json.dumps(request))
    if row['vibe_request_id'] is not None:
        request = {'id': row['vibe_request_id'], 'vibe': row['vibe_preset'], 'phase': row['vibe_phase']}
        worker_settings.save(conn, recommendation_requests.HOME_VIBE, json.dumps(request))
    if row['offer_window'] is not None:
        worker_settings.save(conn, home_offers.KEY, row['offer_window'])
    if row['revisit_remaining'] is not None:
        worker_settings.save(conn, home_revisit_progress.KEY, str(row['revisit_remaining']))


def install(conn, _context):
    _copy_home(conn)
    for table, key, items in ((RETIRED[1], CHECKPOINT, 'weights'), (RETIRED[2], BASELINE, 'entries')):
        row = conn.execute(f'SELECT * FROM {table} WHERE id=1').fetchone()
        if row is not None:
            header = dict(row)
            header[items] = json.loads(header[items])
            reference = tuple(header.pop(field) for field in taste_control_state.REFERENCE_FIELDS)
            taste_control_state.save(conn, key, header, reference)
    for name, _ in _guards():
        conn.execute(f'DROP TRIGGER {name}')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    for _, sql in _guards():
        conn.execute(sql)
    collection_recovery_epoch.install(conn)


def validate(conn):
    guards = dict(_guards())
    schema = definitions(conn, (*RETIRED, *guards))
    if any(table in schema for table in RETIRED) or any(
            tokens(schema.get(name)) != tokens(sql) for name, sql in guards.items()):
        raise MigrationError('Incompatible Home and saved taste settings')
    collection_recovery_epoch.validate(conn)
