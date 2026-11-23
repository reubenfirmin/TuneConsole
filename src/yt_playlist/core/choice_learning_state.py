"""Store the current serving document for each choice learner."""
import json
import math

from . import group_evidence, recommendation_outputs, worker_settings
from .db_revisions import setting_reads

SURFACES = frozenset({'choice_bandit', 'group_choice_bandit', 'choice_interests'})
HEADER = ('schema', 'policy', 'generation', 'state_version', 'at', 'since', 'expires_at',
          'offered', 'picked', 'watermark', 'contexts_total', 'interest_version',
          'parameters', 'excluded', 'rows', 'contexts', 'album_track_days')
SCALARS = HEADER[:12]
PARAMETERS = ('choice', 'open', 'start', 'completion', 'promotion', 'early_exit', 'window_d', 'match_threshold')
EXCLUSIONS = ('unseen', 'unmapped_mode', 'drifted_mode', 'future_context', 'missing_context',
              'unmapped_definition', 'legacy_ambiguous')
MODE = ('mode_id', 'label', 'positive', 'negative', 'days', 'menus', 'estimate', 'low', 'high')
GROUP = ('group_id', 'version', 'positive', 'negative', 'offered', 'picked', 'days',
         'shared_strength', 'shared_from', 'alpha', 'beta', 'estimate', 'menus')
CONTEXT = ('format', 'option_id', 'mode_id', 'intent', 'basis', 'at', 'episode',
           'opportunity_id', 'days', 'episodes', 'episode_support', 'feature', 'keys')
ATTRIBUTION = ('choice_action_id', 'choice_account_id', 'choice_start_at', 'sustained')
MAX_ROWS = 8192
MAX_BYTES = 32 * 1024 * 1024
PREFIX = 'choice_learned:'
KEYS = tuple(PREFIX + surface for surface in sorted(SURFACES))
ID = 'choice_learning_id_high_water'
DESCRIPTORS = f"""SELECT c.value->>'$.descriptor_id'
    FROM settings p,json_each(p.value,'$.document.contexts') c
    WHERE p.key IN {KEYS} AND c.value->>'$.descriptor_id' IS NOT NULL"""


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError('Unsupported choice learning fields')


def _number(value):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError('Invalid choice learning number')


def _context_fields(row):
    return {key: item for key, item in row.items() if key != 'descriptor_id' and key not in ATTRIBUTION}


def _attribution_fields(value):
    return ATTRIBUTION if value.get('schema', 1) >= 3 else ()


def _rows(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError('Choice learning relation exceeds its budget')
    return value


def decode(surface, raw):
    if not isinstance(raw, str) or not 0 < len(raw.encode()) <= MAX_BYTES:
        raise ValueError('Choice learning byte budget exceeded')
    return validate(surface, recommendation_outputs.decode_json(raw))


def validate(surface, value):
    if surface not in SURFACES:
        raise ValueError('Unknown choice learning owner')
    _fields(value, HEADER)
    allowed = (set(SCALARS) | {'contexts', 'album_track_days'} if surface == 'choice_interests'
               else set(SCALARS) | {'parameters', 'excluded', 'rows'})
    if set(value) - allowed:
        raise ValueError('Choice learning fields belong to another owner')
    for field in ('schema', 'at', 'since', 'expires_at', 'offered', 'picked', 'contexts_total'):
        _number(value.get(field))
    for field in ('policy', 'generation', 'state_version', 'watermark', 'interest_version'):
        item = value.get(field)
        if item is not None and (not isinstance(item, str) or len(item.encode()) > 1024):
            raise ValueError('Invalid choice learning identity')
    for field, names in (('parameters', PARAMETERS), ('excluded', EXCLUSIONS)):
        data = value.get(field, {})
        _fields(data, names)
        for item in data.values():
            _number(item)
            if item is None or field == 'excluded' and (type(item) is not int or item < 0):
                raise ValueError('Invalid choice learning parameter or exclusion')
    fields = GROUP if surface == 'group_choice_bandit' else MODE
    opportunities = 0
    for row in _rows(value.get('rows', []), MAX_ROWS):
        _fields(row, fields)
        for key, item in row.items():
            if key not in ('mode_id', 'group_id', 'version', 'label', 'shared_from', 'menus'):
                _number(item)
        if surface == 'group_choice_bandit':
            for key in ('group_id', 'version', 'shared_from'):
                item = row.get(key)
                if item is not None and (not isinstance(item, str) or not 0 < len(item.encode()) <= 1024):
                    raise ValueError('Invalid choice group identity')
            menus = _rows(row.get('menus', []), MAX_ROWS)
            opportunities += len(menus)
            if opportunities > 131072:
                raise ValueError('Choice learning opportunity budget exceeded')
            for menu in menus:
                if not isinstance(menu, str) or not menu or len(menu.encode()) > 1024:
                    raise ValueError('Invalid group learning opportunity')
        else:
            if row.get('mode_id') is not None and type(row['mode_id']) is not int:
                raise ValueError('Invalid legacy choice mode')
            if row.get('label') is not None and (not isinstance(row['label'], str) or len(row['label'].encode()) > 4096):
                raise ValueError('Invalid choice mode label')
            _number(row.get('menus'))
    for row in _rows(value.get('contexts', []), 128):
        _fields(_context_fields(row), CONTEXT)
        if set(row).intersection(ATTRIBUTION) - set(_attribution_fields(value)):
            raise ValueError('Choice attribution requires its projection version')
        if row.get('descriptor_id') is not None and (row.get('format') != 'mixes'
                or type(row['descriptor_id']) is not int or row['descriptor_id'] <= 0):
            raise ValueError('Invalid mix interest descriptor')
        if row.get('format') == 'mixes' and row.get('descriptor_id') is not None and (
                row.get('keys') or row.get('feature') or row.get('mode_id') is not None):
            raise ValueError('Mix interest cannot own track or historical mode precision')
        for key in ('format', 'option_id', 'intent', 'basis', 'episode', 'opportunity_id', 'choice_action_id'):
            item = row.get(key)
            if item is not None and (not isinstance(item, str) or len(item.encode()) > 4096):
                raise ValueError('Invalid choice interest context')
        if row.get('mode_id') is not None and type(row['mode_id']) not in (int, str):
            raise ValueError('Invalid choice interest mode')
        for key in ('at', 'days', 'episodes', 'episode_support', 'choice_start_at'):
            _number(row.get(key))
        if row.get('choice_account_id') is not None and type(row['choice_account_id']) is not int:
            raise ValueError('Invalid choice account')
        if row.get('sustained') is not None and type(row['sustained']) is not bool:
            raise ValueError('Invalid sustained choice indication')
        feature = row.get('feature')
        if feature is not None:
            for number in _rows(feature, 8192):
                _number(number)
                if number is None:
                    raise ValueError('Missing choice feature coordinate')
        for key in _rows(row.get('keys', []), 100):
            if not isinstance(key, str) or not key or len(key.encode()) > 1024:
                raise ValueError('Invalid choice interest recording key')
    albums = value.get('album_track_days', {})
    if not isinstance(albums, dict) or len(albums) > 128:
        raise ValueError('Choice album interest budget exceeded')
    for browse, tracks in albums.items():
        if not isinstance(browse, str) or not 0 < len(browse.encode()) <= 1024 or not isinstance(tracks, dict) or len(tracks) > 1000:
            raise ValueError('Invalid choice album interest')
        for key, days in tracks.items():
            if not isinstance(key, str) or not 0 < len(key.encode()) <= 1024 or type(days) is not int or days < 0:
                raise ValueError('Invalid choice album listening days')
    return value


def encode(value):
    if 'contexts' in value:
        nullable = {'descriptor_id', *ATTRIBUTION}
        value = dict(value, contexts=[
            {key: item for key, item in row.items() if item is not None or key not in nullable}
            for row in value['contexts']
        ])
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def unchanged_group_report(previous, value, now):
    """Retain the original cohort when only its valid capture time advances.

    Estimates, policy, parameters, diagnostics and expiry must all agree. Keep
    unknown clocks and future/rewound captures on the ordinary publication path.
    """
    if not isinstance(previous, dict) or not isinstance(value, dict):
        return False
    if previous == value:
        return True
    clocks = previous.get('at'), value.get('at'), now
    if not all(type(clock) in (int, float) and math.isfinite(clock) for clock in clocks):
        return False
    return (clocks[0] <= clocks[1] <= clocks[2]
            and {key: item for key, item in previous.items() if key != 'at'}
                == {key: item for key, item in value.items() if key != 'at'})


def write(conn, surface, value, at):
    if not conn.in_transaction:
        raise ValueError('Choice learning publication requires the outer transaction')
    validate(surface, value)
    _number(at)
    key = PREFIX + surface
    raw = worker_settings.read(conn, key)
    previous = json.loads(raw) if raw else None
    if previous is not None:
        if surface == 'group_choice_bandit' and unchanged_group_report(previous['document'], value, at):
            return previous['id']
        group_evidence.release(conn, 'prepared', f"choice-learning:{previous['id']}")
    identity = int(worker_settings.read(conn, ID, '0')) + 1
    worker_settings.save(conn, ID, str(identity))
    save(conn, surface, identity, at, encode(value))
    return identity


def save(conn, surface, identity, at, document):
    row = json.dumps({'id': identity, 'built_at': at, 'document': json.loads(document)}, separators=(',', ':'))
    worker_settings.save(conn, PREFIX + surface, row)


def read(conn, surface, *, store=None):
    key = PREFIX + surface
    with setting_reads(store, (key,)):
        row = conn.execute("SELECT value->'$.document' FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row is not None else None


def _selected_fields(fields, row):
    return {key: json.loads(value) for key, value in zip(fields, row, strict=True) if value is not None}


def refresh_input(conn, surface, *, store=None):
    """Read ranking keys and decay inputs without transferring learned vectors or songs."""
    if surface == 'choice_bandit':
        fields = ('state_version', 'expires_at')
    elif surface == 'choice_interests':
        fields = ('interest_version', 'schema', 'policy')
    else:
        raise ValueError('Unknown choice refresh input')
    columns = ','.join('value -> ?' for _ in fields)
    key = PREFIX + surface
    with setting_reads(store, (key,)):
        row = conn.execute("SELECT json_type(value,'$.document.contexts')," + columns +
                           ' FROM settings WHERE key=?', (*(f'$.document.{field}' for field in fields), key)).fetchone()
        if row is None:
            return None
        result = _selected_fields(fields, row[1:])
        if row[0] == 'array':
            context_fields = ('format', 'at', 'days', 'episodes', 'episode_support', 'descriptor_id', *ATTRIBUTION)
            columns = ','.join('c.value -> ?' for _ in context_fields)
            contexts = conn.execute('SELECT ' + columns + ' FROM settings p,'
                                    "json_each(p.value,'$.document.contexts') c WHERE p.key=? ORDER BY c.key",
                                    (*context_fields, key))
            result['contexts'] = [_selected_fields(context_fields, context) for context in contexts]
    return result


def present(conn, surface, *, store=None):
    key = PREFIX + surface
    with setting_reads(store, (key,)):
        return conn.execute("SELECT 1 FROM settings WHERE key=? AND value->'$.document'<>'{}'",
                            (key,)).fetchone() is not None


def clear(conn):
    """Retire serving projections; the observations remain in the choice ledger."""
    if not conn.in_transaction:
        raise ValueError('Choice learning invalidation requires the outer transaction')
    for key in KEYS:
        row = conn.execute("SELECT value->>'id' FROM settings WHERE key=?", (key,)).fetchone()
        if row:
            group_evidence.release(conn, 'prepared', f'choice-learning:{row[0]}')
            worker_settings.save(conn, key, None)
