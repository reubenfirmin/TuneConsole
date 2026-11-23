"""Content preparation checkpoints and input revisions in settings."""
import json
from math import isfinite

from .content_checkpoint_contract import CONFIG, FIELDS, KEYS, MODES, PHASES
from .worker_settings import read, save


CHECKPOINTS = {key: f'content_preparation_{phase}' for key, phase in KEYS.items()}
CONTENT_REVISION = 'content_input_revision'
EVIDENCE_REVISION = 'recommendation_evidence_revision'
REFRESH_REVISION = 'recommendation_refresh_revision'
ACKNOWLEDGED = 'recommendation_refresh_acknowledged'
FIT_VERSION = 'content_fit_version'


def get(conn, key, default=None):
    return read(conn, CHECKPOINTS[key], default)


def _nonnegative(value):
    return value is None or type(value) is int and 0 <= value < 2 ** 63


def put(conn, values):
    for key, raw in values.items():
        phase = KEYS[key]
        value = json.loads(raw) if raw else {}
        allowed = set(FIELDS[key]) | ({'config'} if phase == 'full' else {'phase_bytes'})
        if phase == 'basis':
            allowed.add('modes')
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError('Unsupported content preparation checkpoint')
        if not raw:
            save(conn, CHECKPOINTS[key], None)
            continue
        if phase == 'fit' and value.get('state') not in (None, 'ready', 'waiting', 'deferred'):
            raise ValueError('Unsupported content fit state')
        for field, field_value in value.items():
            if field_value is None:
                continue
            if field in ('at', 'attempted_at', 'since', 'next_attempt_at'):
                if type(field_value) not in (int, float) or not isfinite(field_value):
                    raise ValueError('Content checkpoint time must be finite')
            elif field in ('reason', 'input_fingerprint', 'state', 'config', 'modes', 'phase_bytes'):
                continue
            elif not _nonnegative(field_value):
                raise ValueError('Content checkpoint counter must be nonnegative')
        config, modes, phases = (value.get(field) for field in ('config', 'modes', 'phase_bytes'))
        if config is not None and (not isinstance(config, list) or len(config) != len(CONFIG)
                or any(item is not None and not isinstance(item, str) for item in config)):
            raise ValueError('Content configuration must retain its seven scalar settings')
        if modes is not None and (not isinstance(modes, list) or len(modes) != len(MODES)
                or any(type(item) is not int or item <= 0 for item in modes[:2])
                or type(modes[2]) not in (int, float) or not 0 <= modes[2] <= 1):
            raise ValueError('Content checkpoint requires three valid mode parameters')
        if phases is not None:
            if (not isinstance(phases, dict) or set(phases) - set(PHASES)
                    or not all(map(_nonnegative, phases.values()))):
                raise ValueError('Unsupported content preparation memory phase')
            value['phase_bytes'] = {name: size for name, size in phases.items() if size is not None}
        value = {field: value for field, value in value.items() if value is not None}
        if phase == 'full':
            value.setdefault('at', None)
        save(conn, CHECKPOINTS[key], json.dumps(value, allow_nan=False))
