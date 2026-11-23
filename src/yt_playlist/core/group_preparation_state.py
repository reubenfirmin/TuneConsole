"""Worker progress before grouping and checkpoints on their generations."""
import json
from math import isfinite

from . import group_progress
from .worker_settings import read, save


PROGRESS_KEY = 'rec_group_shadow_progress'
CHECKPOINT_KEY = 'rec_group_shadow_checkpoint'
FIT_KEY = 'rec_mode_fit_inputs'
KEYS = frozenset({PROGRESS_KEY, CHECKPOINT_KEY, FIT_KEY})
SERIAL = 'group_input_serial'
ROOTS = 'group_retention_roots_complete'
FIT = 'group_fit_fingerprint'
UNRESOLVED = 'group_unresolved_checkpoint'


def get(conn, key, default=None):
    if key == PROGRESS_KEY:
        return group_progress.read(conn, default)
    if key == FIT_KEY:
        return read(conn, FIT, default)
    row = conn.execute('SELECT external_key,worker_input_serial,worker_checkpoint_at FROM group_generations '
                       'WHERE worker_checkpoint_at IS NOT NULL').fetchone()
    if row is None:
        return read(conn, UNRESOLVED, default)
    return json.dumps(dict(zip(('generation', 'serial', 'at'), row, strict=True)))


def put(conn, values):
    for key, raw in values.items():
        if key == FIT_KEY:
            save(conn, FIT, raw)
            continue
        value = json.loads(raw) if raw else {}
        if not isinstance(value, dict):
            raise ValueError('Grouping progress must be a supported object')
        if key == PROGRESS_KEY:
            group_progress.write(conn, value)
        else:
            if set(value) - {'generation', 'serial', 'at'}:
                raise ValueError('Unsupported grouping checkpoint')
            checkpoint(conn, value.get('generation'), value.get('serial'), value.get('at'))


def checkpoint(conn, generation, serial, now):
    if (generation, serial, now) != (None, None, None):
        if (not isinstance(generation, str) or type(serial) is not int or not 0 <= serial < 2 ** 63
                or type(now) not in (int, float) or not isfinite(now)):
            raise ValueError('Grouping checkpoint requires a generation, input revision and time')
        identity = conn.execute('SELECT id FROM group_generations WHERE external_key=?', (generation,)).fetchone()
        if identity is None:
            raise ValueError('Grouping checkpoint requires its published generation')
        identity = identity[0]
    else:
        identity = None
    conn.execute('UPDATE group_generations SET worker_input_serial=NULL,worker_checkpoint_at=NULL '
                 'WHERE worker_checkpoint_at IS NOT NULL AND id IS NOT ?', (identity,))
    if identity is not None:
        conn.execute('UPDATE group_generations SET worker_input_serial=?,worker_checkpoint_at=? WHERE id=?',
                     (serial, now, identity))
    save(conn, UNRESOLVED, None)
