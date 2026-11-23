"""Keep confirmed TV draw history alongside TV preparation."""
import json

from .. import recommendation_work
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'own_tv_draw_history'
TABLE = 'tv_preparation_state'
KEYS = ('tv_draw', 'tv_recent_videos', 'tv_last_videos')


def predecessor_objects(conn):
    return list(definitions(conn, (TABLE,)).values())


def install(conn, _context):
    for column in recommendation_work.TV_DRAW_COLUMNS:
        conn.execute(f'ALTER TABLE {TABLE} ADD COLUMN {column}')
    values = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?,?)', KEYS))
    if values:
        try:
            number = max(0, int(values.get('tv_draw') or 0))
        except (ValueError, TypeError):
            number = 0
        recent = recommendation_work.video_history(values.get('tv_recent_videos'), recommendation_work.TV_POOL_LIMIT)
        last = recommendation_work.video_history(values.get('tv_last_videos'), recommendation_work.TV_QUEUE_LIMIT)
        conn.execute(f'INSERT INTO {TABLE}(id,failed,updated_at,pool_state_present,draw_number,recent_videos,last_videos) '
            'VALUES (1,0,0,0,?,?,?) ON CONFLICT(id) DO UPDATE SET draw_number=excluded.draw_number,'
            'recent_videos=excluded.recent_videos,last_videos=excluded.last_videos', (number, json.dumps(recent), json.dumps(last)))
        conn.execute('DELETE FROM settings WHERE key IN (?,?,?)', KEYS)


def validate(conn):
    layout = tokens(definitions(conn, (TABLE,)).get(TABLE))
    if not all(column.split()[0] in layout for column in recommendation_work.TV_DRAW_COLUMNS):
        raise MigrationError('Invalid TV draw storage')
