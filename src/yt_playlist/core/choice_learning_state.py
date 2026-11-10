"""Relational serving inputs compiled from choice evidence, independently replaceable.

The public dictionaries are compatibility views over fixed numerical fields and
ordered relations. They are never stored as JSON or searched for references.
"""
import math
import time
from contextlib import ExitStack

from . import group_evidence, recommendation_outputs, mix_choices
from .db_revisions import keyed_reads

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
MAX_ROWS = 8192
MAX_BYTES = 32 * 1024 * 1024
READ_TABLES = ('choice_learning_publications', 'choice_learning_parameters', 'choice_learning_exclusions',
               'choice_mode_estimates', 'choice_group_estimates', 'choice_group_opportunities',
               'choice_interest_contexts', 'choice_interest_features', 'choice_interest_tracks',
               'choice_album_interests', 'choice_album_track_days', 'choice_interest_mix_descriptors')


def _mask(value, fields):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError('Unsupported choice learning fields')
    return sum(1 << index for index, field in enumerate(fields) if field in value)


def _view(row, fields):
    return {field: row[index + 1] for index, field in enumerate(fields) if row[0] & (1 << index)}


def _number(value):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError('Invalid choice learning number')


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
    _mask(value, HEADER)
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
        _mask(data, names)
        for item in data.values():
            _number(item)
            if item is None or field == 'excluded' and (type(item) is not int or item < 0):
                raise ValueError('Invalid choice learning parameter or exclusion')
    fields = GROUP if surface == 'group_choice_bandit' else MODE
    opportunities = 0
    for row in _rows(value.get('rows', []), MAX_ROWS):
        _mask(row, fields)
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
        _mask({key: item for key, item in row.items() if key != 'descriptor_id'}, CONTEXT)
        if row.get('descriptor_id') is not None and (row.get('format') != 'mixes'
                or type(row['descriptor_id']) is not int or row['descriptor_id'] <= 0):
            raise ValueError('Invalid mix interest descriptor')
        if row.get('format') == 'mixes' and row.get('descriptor_id') is not None and (
                row.get('keys') or row.get('feature') or row.get('mode_id') is not None):
            raise ValueError('Mix interest cannot own track or historical mode precision')
        for key in ('format', 'option_id', 'intent', 'basis', 'episode', 'opportunity_id'):
            item = row.get(key)
            if item is not None and (not isinstance(item, str) or len(item.encode()) > 4096):
                raise ValueError('Invalid choice interest context')
        if row.get('mode_id') is not None and type(row['mode_id']) not in (int, str):
            raise ValueError('Invalid choice interest mode')
        for key in ('at', 'days', 'episodes', 'episode_support'):
            _number(row.get(key))
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


def _insert(conn, table, columns, values):
    return conn.execute(f'INSERT INTO {table}({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', values)


def _recording(conn, key):
    row = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
    return row[0] if row else None


def _album(conn, browse):
    row = conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?", (browse,)).fetchone()
    return row[0] if row else None


def write(conn, surface, value, at, *, historical=False):
    if not conn.in_transaction:
        raise ValueError('Choice learning publication requires the outer transaction')
    validate(surface, value)
    _number(at)
    previous = conn.execute('SELECT publication_id FROM choice_learning_heads WHERE surface=?', (surface,)).fetchone()
    columns = ('surface', 'field_mask', 'built_at', *SCALARS)
    # Public schema is a field name, not the SQL schema keyword.
    sql_columns = tuple('schema_version' if field == 'schema' else field for field in columns)
    identity = _insert(conn, 'choice_learning_publications', sql_columns,
                       (surface, _mask(value, HEADER), at, *(value.get(field) for field in SCALARS))).lastrowid
    for field, table in (('parameters', 'choice_learning_parameters'), ('excluded', 'choice_learning_exclusions')):
        conn.executemany(f'INSERT INTO {table} VALUES (?,?,?)',
                         [(identity, key, item) for key, item in value.get(field, {}).items()])
    if surface in ('choice_bandit', 'group_choice_bandit'):
        fields, table = (GROUP, 'choice_group_estimates') if surface == 'group_choice_bandit' else (MODE, 'choice_mode_estimates')
        scalar = fields[:-1] if surface == 'group_choice_bandit' else fields
        for position, row in enumerate(value.get('rows', [])):
            columns = ('publication_id', 'position', 'field_mask', *scalar)
            values = (identity, position, _mask(row, fields), *(row.get(field) for field in scalar))
            if surface == 'choice_bandit':
                mode = conn.execute('SELECT mode_id FROM taste_modes WHERE mode_id=?', (row.get('mode_id'),)).fetchone()
                columns += ('resolved_mode_id', 'resolution_reason')
                values += (mode[0] if mode else None, None if mode else 'missing_mode')
            _insert(conn, table, columns, values)
            if surface == 'group_choice_bandit':
                conn.executemany('INSERT INTO choice_group_opportunities VALUES (?,?,?,?)',
                    [(identity, position, index, menu) for index, menu in enumerate(row.get('menus', []))])
    for position, row in enumerate(value.get('contexts', [])):
        scalar = CONTEXT[:-2]
        album = _album(conn, row.get('option_id')) if row.get('format') == 'albums' else None
        _insert(conn, 'choice_interest_contexts', ('publication_id', 'position', 'field_mask', *scalar,
                                                'feature_null', 'album_id', 'resolution_reason'),
                (identity, position, _mask({key: item for key, item in row.items() if key != 'descriptor_id'}, CONTEXT), *(row.get(field) for field in scalar),
                 int(row.get('feature') is None), album,
                 'missing_album' if row.get('format') == 'albums' and album is None else None))
        if row.get('descriptor_id') is not None:
            conn.execute('INSERT INTO choice_interest_mix_descriptors VALUES (?,?,?)',
                         (identity, position, row['descriptor_id']))
        conn.executemany('INSERT INTO choice_interest_features VALUES (?,?,?,?)',
                         [(identity, position, index, number) for index, number in enumerate(row.get('feature') or [])])
        conn.executemany('INSERT INTO choice_interest_tracks VALUES (?,?,?,?,?,?)',
                         [(identity, position, index, key, recording,
                           None if recording else 'missing_recording')
                          for index, key in enumerate(row.get('keys', [])) for recording in [_recording(conn, key)]])
    for position, (browse, tracks) in enumerate(value.get('album_track_days', {}).items()):
        album = _album(conn, browse)
        conn.execute('INSERT INTO choice_album_interests VALUES (?,?,?,?,?)',
                     (identity, position, browse, album, None if album else 'missing_album'))
        for rank, (key, days) in enumerate(tracks.items()):
            recording = _recording(conn, key)
            conn.execute('INSERT INTO choice_album_track_days VALUES (?,?,?,?,?,?,?)',
                         (identity, position, rank, key, recording, days, None if recording else 'missing_recording'))
    conn.execute('INSERT INTO choice_learning_heads VALUES (?,?) ON CONFLICT(surface) '
                 'DO UPDATE SET publication_id=excluded.publication_id', (surface, identity))
    if previous:
        conn.execute('DELETE FROM choice_learning_group_roots WHERE publication_id=?', (previous[0],))
        group_evidence.release(conn, 'prepared', f'choice-learning:{previous[0]}')
        conn.execute('DELETE FROM choice_learning_publications WHERE id=?', (previous[0],))
    return identity


def read(conn, surface, *, store=None):
    with keyed_reads(store, 'choice_learning_heads', (surface,)):
        head = conn.execute('SELECT publication_id FROM choice_learning_heads WHERE surface=?', (surface,)).fetchone()
    if head is None:
        return None
    with ExitStack() as reads:
        for table in READ_TABLES:
            reads.enter_context(keyed_reads(store, table, (head[0],)))
        return _read(conn, surface, head[0])


def refresh_input(conn, surface, *, store=None):
    """Ranking freshness needs identities and decay inputs, without learned vectors or songs."""
    if surface not in {'choice_interests', 'choice_bandit'}:
        raise ValueError('Unknown choice refresh input')
    with keyed_reads(store, 'choice_learning_heads', (surface,)):
        head = conn.execute('SELECT publication_id FROM choice_learning_heads WHERE surface=?', (surface,)).fetchone()
    if head is None:
        return None
    identity = head[0]
    with keyed_reads(store, 'choice_learning_publications', (identity,)):
        columns = ','.join('schema_version' if field == 'schema' else field for field in SCALARS)
        row = conn.execute('SELECT field_mask,' + columns + ' FROM choice_learning_publications WHERE id=?',
                           (identity,)).fetchone()
    fields = {'interest_version'} if surface == 'choice_interests' else {'state_version', 'expires_at'}
    result = {key: value for key, value in _view(row, SCALARS).items() if key in fields}
    if surface == 'choice_interests' and row[0] & (1 << HEADER.index('contexts')):
        with keyed_reads(store, 'choice_interest_contexts', (identity,)):
            scalar = CONTEXT[:-2]
            records = conn.execute(f'SELECT position,field_mask,{",".join(scalar)} '
                                   'FROM choice_interest_contexts WHERE publication_id=? ORDER BY position',
                                   (identity,)).fetchall()
        descriptors = {}
        if mix_choices.available(conn):
            with keyed_reads(store, 'choice_interest_mix_descriptors', (identity,)):
                descriptors = dict(conn.execute('SELECT context_position,descriptor_id '
                    'FROM choice_interest_mix_descriptors WHERE publication_id=?', (identity,)))
        result['contexts'] = []
        for position, *record in records:
            context = {key: value for key, value in _view(record, scalar).items()
                       if key in {'format', 'at', 'days', 'episode_support'}}
            if position in descriptors:
                context['descriptor_id'] = descriptors[position]
            result['contexts'].append(context)
    return result


def present(conn, surface, *, store=None):
    with keyed_reads(store, 'choice_learning_heads', (surface,)):
        head = conn.execute('SELECT publication_id FROM choice_learning_heads WHERE surface=?', (surface,)).fetchone()
    if head is None:
        return False
    with keyed_reads(store, 'choice_learning_publications', (head[0],)):
        return conn.execute('SELECT field_mask>0 FROM choice_learning_publications WHERE id=?', (head[0],)).fetchone()[0] == 1


def _read(conn, surface, identity, *, prefix=''):
    if prefix not in ('', 'prepared_'):
        raise ValueError('Unknown choice learning relation namespace')
    def table(name):
        return prefix + name
    columns = ','.join('p.schema_version' if field == 'schema' else 'p.' + field for field in SCALARS)
    row = conn.execute('SELECT p.id,p.field_mask,' + columns +
                       ' FROM '+table('choice_learning_publications')+' p WHERE p.id=?', (identity,)).fetchone()
    identity, mask = row[:2]
    result = _view(row[1:], SCALARS)
    def present(field):
        return bool(mask & (1 << HEADER.index(field)))
    for field, owner in (('parameters', 'choice_learning_parameters'), ('excluded', 'choice_learning_exclusions')):
        if present(field):
            result[field] = dict(conn.execute(f'SELECT name,value FROM {table(owner)} WHERE publication_id=? ORDER BY name', (identity,)))
    if present('rows'):
        fields, owner = (GROUP, 'choice_group_estimates') if surface == 'group_choice_bandit' else (MODE, 'choice_mode_estimates')
        scalar = fields[:-1] if surface == 'group_choice_bandit' else fields
        result['rows'] = []
        for position, *record in conn.execute(f'SELECT position,field_mask,{",".join(scalar)} FROM {table(owner)} '
                                               'WHERE publication_id=? ORDER BY position', (identity,)):
            value = _view(record, scalar)
            if surface == 'group_choice_bandit' and record[0] & (1 << GROUP.index('menus')):
                value['menus'] = [entry[0] for entry in conn.execute('SELECT opportunity_id FROM '+table('choice_group_opportunities')+' '
                    'WHERE publication_id=? AND estimate_position=? ORDER BY position', (identity, position))]
            result['rows'].append(value)
    if present('contexts'):
        result['contexts'] = []
        scalar = CONTEXT[:-2]
        for position, *record in conn.execute(f'SELECT position,field_mask,{",".join(scalar)},feature_null FROM {table("choice_interest_contexts")} '
                                               'WHERE publication_id=? ORDER BY position', (identity,)):
            value = _view(record, scalar)
            if record[0] & (1 << CONTEXT.index('feature')):
                value['feature'] = None if record[-1] else [entry[0] for entry in conn.execute('SELECT value FROM '+table('choice_interest_features')+' '
                    'WHERE publication_id=? AND context_position=? ORDER BY dimension', (identity, position))]
            if record[0] & (1 << CONTEXT.index('keys')):
                value['keys'] = [entry[0] for entry in conn.execute('SELECT raw_key FROM '+table('choice_interest_tracks')+' '
                    'WHERE publication_id=? AND context_position=? ORDER BY position', (identity, position))]
            if mix_choices.available(conn):
                descriptor = conn.execute('SELECT descriptor_id FROM '+table('choice_interest_mix_descriptors')+' '
                    'WHERE publication_id=? AND context_position=?', (identity, position)).fetchone()
                if descriptor:
                    value['descriptor_id'] = descriptor[0]
            result['contexts'].append(value)
    if present('album_track_days'):
        result['album_track_days'] = {browse: dict(conn.execute('SELECT raw_key,days FROM '+table('choice_album_track_days')+' '
            'WHERE publication_id=? AND album_position=? ORDER BY position', (identity, position)))
            for position, browse in conn.execute('SELECT position,browse_id FROM '+table('choice_album_interests')+' '
                'WHERE publication_id=? ORDER BY position', (identity,))}
    return result


def clear(conn):
    """Retire serving projections; the observations remain in the choice ledger."""
    if not conn.in_transaction:
        raise ValueError('Choice learning invalidation requires the outer transaction')
    identities = [row[0] for row in conn.execute('SELECT publication_id FROM choice_learning_heads')]
    conn.execute('DELETE FROM choice_learning_heads')
    for identity in identities:
        conn.execute('DELETE FROM choice_learning_group_roots WHERE publication_id=?', (identity,))
        group_evidence.release(conn, 'prepared', f'choice-learning:{identity}')
        conn.execute('DELETE FROM choice_learning_publications WHERE id=?', (identity,))
