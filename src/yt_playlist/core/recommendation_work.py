"""Fixed work-state contracts for daily model selection and TV preparation."""
import math
import json

SURFACES = ('daily_model_schedule', 'tv_preparation')


def prepared_owner(conn):
    # The genuine historical owner takes precedence after an explicit frozen
    # upgrade. Publication never collides with its table names or stamps history.
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='model_selection_schedule'").fetchone():
        return False
    return bool(conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='published_work'").fetchone()
                and conn.execute('SELECT 1 FROM published_work WHERE id=1').fetchone())


def available(conn):
    return bool(conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='model_selection_schedule'").fetchone()
                or prepared_owner(conn))


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Expected a finite numeric timestamp')
    return value


def parse(surface, value, built_at):
    finite(built_at)
    if not isinstance(value, dict):
        raise ValueError('Recommendation work state must be an object')
    if surface == 'daily_model_schedule':
        if set(value) - {'next_at'}:
            raise ValueError('Unknown daily schedule field')
        return (finite(value['next_at']), built_at) if value else None
    if surface == 'tv_preparation':
        if set(value) - {'failed', 'at'} or (value and type(value.get('failed')) is not bool):
            raise ValueError('Invalid TV preparation state')
        return (int(value['failed']), finite(value['at']) if 'at' in value else None, built_at) if value else None
    raise ValueError('Unknown recommendation work owner')


def write(conn, surface, value, built_at):
    row = parse(surface, value, built_at)
    if prepared_owner(conn):
        # Publication triggers validate even writes from older connections and
        # atomically maintain the typed owner alongside its retained document.
        conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                     'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                     (surface, json.dumps(value), built_at))
        return
    if surface == 'daily_model_schedule':
        if row is None:
            conn.execute('DELETE FROM model_selection_schedule')
        else:
            conn.execute('INSERT INTO model_selection_schedule VALUES (1,?,?) '
                         'ON CONFLICT(id) DO UPDATE SET next_at=excluded.next_at,updated_at=excluded.updated_at', row)
    elif row is None:
        conn.execute('DELETE FROM tv_preparation_state')
    else:
        conn.execute('INSERT INTO tv_preparation_state VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                     'failed=excluded.failed,observed_at=excluded.observed_at,updated_at=excluded.updated_at', row)


def read(conn, surface):
    prefix = 'prepared_' if prepared_owner(conn) else ''
    if surface == 'daily_model_schedule':
        row = conn.execute('SELECT next_at FROM ' + prefix + 'model_selection_schedule WHERE id=1').fetchone()
        return {'next_at': row[0]} if row else None
    if surface == 'tv_preparation':
        row = conn.execute('SELECT failed,observed_at FROM ' + prefix + 'tv_preparation_state WHERE id=1').fetchone()
        return ({'failed': bool(row[0]), **({'at': row[1]} if row[1] is not None else {})} if row else None)
    raise ValueError('Unknown recommendation work owner')
