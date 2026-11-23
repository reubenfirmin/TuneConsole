"""Keep explicit experiment decisions and their captured evidence together."""
import json
import math

from . import worker_settings


KEY = 'ppr_experiment_decisions'
HISTORY = 'experiment_decision_history'
KEYS = frozenset({KEY})
LIMIT = 200


def get(conn, key, default=None):
    return worker_settings.read(conn, HISTORY, default)


def records(conn):
    return json.loads(get(conn, KEY, '[]'))


def save(conn, values):
    if values:
        worker_settings.save(conn, HISTORY, json.dumps(values, separators=(',', ':'), allow_nan=False))


def put(conn, values):
    current = records(conn)
    value = json.loads(values[KEY]) if values[KEY] else []
    if not isinstance(value, list) or len(value) > LIMIT:
        raise ValueError('Experiment decisions require at most 200 ordered records')
    if value == current:
        return
    if len(value) != min(LIMIT, len(current) + 1) or value[:-1] != current[-(LIMIT - 1):]:
        raise ValueError('Published experiment decisions cannot be rewritten')
    item = value[-1]
    if (not isinstance(item, dict) or set(item) != {'decision', 'cohort', 'reason', 'at', 'powered', 'basis', 'live_evidence'}
            or item['decision'] not in ('promote', 'retire')
            or not isinstance(item['cohort'], str) or len(item['cohort']) > 100
            or not isinstance(item['reason'], str) or not 1 <= len(item['reason'].strip()) <= 2000
            or type(item['at']) not in (int, float) or not math.isfinite(item['at'])
            or type(item['powered']) is not bool or not isinstance(item['basis'], str)
            or not isinstance(item['live_evidence'], dict) or set(item['live_evidence']) != {'ppr', 'cosine'}):
        raise ValueError('Unsupported experiment decision')
    save(conn, value)
