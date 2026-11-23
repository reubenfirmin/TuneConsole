"""Fixed work-state contracts for daily model selection and TV preparation."""
import json
import math
import re

from . import worker_settings
from .db_revisions import setting_reads

SURFACES = ('daily_model_schedule', 'tv_preparation')
TV_POOL_LIMIT = 60
TV_QUEUE_LIMIT = 12
TV_DRAW_KEY = 'tv_draw'
TV_DRAW_COLUMNS = (
    'pool_state_present INTEGER NOT NULL DEFAULT 1 CHECK(pool_state_present IN (0,1))',
    "draw_number INTEGER NOT NULL DEFAULT 0 CHECK(typeof(draw_number)='integer' AND draw_number>=0)",
    "recent_videos TEXT NOT NULL DEFAULT '[]' CHECK(json_type(recent_videos) IS 'array' "
    f'AND json_array_length(recent_videos)<={TV_POOL_LIMIT})',
    "last_videos TEXT NOT NULL DEFAULT '[]' CHECK(json_type(last_videos) IS 'array' "
    f'AND json_array_length(last_videos)<={TV_QUEUE_LIMIT})',
)
_VIDEO = re.compile(r'^[A-Za-z0-9_-]{11}$')


def video_history(raw, limit):
    try:
        values = json.loads(raw or '[]')
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    return [video for video in values[-limit:] if isinstance(video, str) and _VIDEO.fullmatch(video)]


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
    if surface == 'daily_model_schedule':
        document = {'next_at': row[0], 'saved_at': row[1]} if row is not None else None
    else:
        document = {**value, 'saved_at': built_at} if row is not None else None
    worker_settings.save(conn, surface, json.dumps(document) if document is not None else None)


def present(conn, surface):
    if surface not in SURFACES:
        raise ValueError('Unknown recommendation work owner')
    with setting_reads(conn, (surface,)):
        return conn.execute('SELECT 1 FROM settings WHERE key=?', (surface,)).fetchone() is not None


def read(conn, surface):
    if surface not in SURFACES:
        raise ValueError('Unknown recommendation work owner')
    raw = worker_settings.read(conn, surface)
    if raw is None:
        return None
    document = json.loads(raw)
    document.pop('saved_at')
    return document
