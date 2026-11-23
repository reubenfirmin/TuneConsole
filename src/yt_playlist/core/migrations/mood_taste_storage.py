"""Move mood seeds and saved taste lists onto their existing owners."""
from itertools import groupby
import json

from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'inline_mood_seeds_and_taste_snapshots'
OWNERS = {'mood_events': 'seeds', 'taste_settings_checkpoints': 'weights', 'taste_comparison_baselines': 'entries'}
RETIRED = ('mood_event_recordings', 'mood_event_unresolved_seeds', 'taste_checkpoint_weights', 'taste_comparison_entries')


def _column(name):
    return f"{name} TEXT NOT NULL DEFAULT '[]' CHECK(json_type({name}) IS 'array')"


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name IN (?,?,?,?,?,?,?) '
        'AND sql IS NOT NULL ORDER BY type,name', (*OWNERS, *RETIRED))]


def _copy(conn, owner, query):
    # Encoding in Python preserves the full precision of snapshot REAL values.
    column = OWNERS[owner]
    for identity, group in groupby(conn.execute(query), key=lambda row: row[0]):
        items = [dict(zip(row.keys()[1:], row[1:], strict=True)) for row in group]
        conn.execute(f'UPDATE {owner} SET {column}=? WHERE id=?', (json.dumps(items, allow_nan=False), identity))


def _guards(conn):
    references = {
        'recordings': (('mood_events', 'seeds', 'recording_id'), ('taste_comparison_baselines', 'entries', 'recording_id')),
        'taste_axes': (('taste_settings_checkpoints', 'weights', 'axis_id'),),
    }
    for parent, children in references.items():
        retained = ' OR '.join(f"EXISTS(SELECT 1 FROM {owner} o,json_each(o.{column}) f WHERE f.value->>'{key}'=OLD.id)"
                               for owner, column, key in children)
        for operation, event, changed in (('delete', 'DELETE', ''), ('rekey', 'UPDATE OF id', 'NEW.id IS NOT OLD.id AND ')):
            conn.execute(f'''CREATE TRIGGER mood_taste_retains_{parent}_{operation} BEFORE {event} ON {parent}
                WHEN {changed}({retained})
                BEGIN SELECT RAISE(ABORT,'A saved mood or taste snapshot retains this identity'); END''')


def install(conn, _context):
    for owner, column in OWNERS.items():
        conn.execute(f'ALTER TABLE {owner} ADD COLUMN {_column(column)}')
    _copy(conn, 'mood_events', '''SELECT mood_event_id,position,recording_id,NULL unresolved_key,NULL reason
        FROM mood_event_recordings UNION ALL SELECT mood_event_id,position,NULL,legacy_key,reason
        FROM mood_event_unresolved_seeds ORDER BY 1,2''')
    _copy(conn, 'taste_settings_checkpoints', '''SELECT checkpoint_id,position,axis_id,captured_axis,weight,updated_at
        FROM taste_checkpoint_weights ORDER BY checkpoint_id,position''')
    _copy(conn, 'taste_comparison_baselines', '''SELECT baseline_id,position,recording_id,unresolved_key,captured_key,
        rank,score,lane,lane_weight FROM taste_comparison_entries ORDER BY baseline_id,position''')
    for table in RETIRED[:2]:
        for suffix in ('insert', 'update'):
            conn.execute(f'DROP TRIGGER {table}_position_{suffix}')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    _guards(conn)


def validate(conn, *, taste_settings=False):
    owners = {'mood_events': OWNERS['mood_events']} if taste_settings else OWNERS
    installed = definitions(conn, (*owners, *RETIRED))
    if (any(table in installed for table in RETIRED)
            or any(_column(column) not in installed.get(owner, '') for owner, column in owners.items())):
        raise MigrationError('Incompatible inline mood and taste lists')
