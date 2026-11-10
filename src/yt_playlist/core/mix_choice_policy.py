"""Frozen mix display values and interpretation of detached menu projections."""
import hashlib

from .evidence_ledger import canonical

PRECISION = 'theme-raw-genres-v1'
GROUP_FIELDS = frozenset(('groups', 'musical_groups', 'musical_definitions', 'group_generation',
                          'group_allocation', 'group_choice_policy', 'group_choice_parameters',
                          'recommendation_generations'))


def visible_option(option):
    if 'descriptor_id' in option:
        return dict(option)
    explicit = 'genres' in option
    tracks = option.get('tracks') or []
    genres = option.get('genres') if explicit else [t['genre'].strip().lower() for t in tracks if (t.get('genre') or '').strip()]
    if (not isinstance(genres, list) or len(genres) > 256
            or any(not isinstance(g, str) or not g or len(g.encode()) > 4096 for g in genres)):
        raise ValueError('Invalid displayed raw genres')
    result = {key: option[key] for key in ('id', 'position', 'label', 'note', 'intent', 'score', 'assignment') if key in option}
    return {**result, 'genres': sorted(set(genres)),
            'genres_complete': bool(option.get('genres_complete', explicit or bool(tracks) and all((t.get('genre') or '').strip() for t in tracks))),
            'genre_origin': option.get('genre_origin', 'rendered' if explicit else 'legacy_tracks')}


def context(value, *, mixes=False):
    if mixes:
        return {'schema': 2, 'precision': PRECISION, **{key: value[key] for key in ('at', 'comparison') if key in value}}
    return {key: item for key, item in value.items() if key not in GROUP_FIELDS}


def menu_plan(record, context_id, *, source=None):
    source = record if source is None else source
    return {'id': record['id'], 'context': context_id, 'source_context': source.get('context') or context_id,
            'source_digest': hashlib.sha256(canonical(source)).digest(),
            'context_at': record.get('context_at', record['at']),
            'options': [visible_option(option) for option in record['options']]}


def project(record, facts):
    """Keep unprepared legacy menus; compact IDs need their SQL publication."""
    if not record or 'context' not in record:
        return record
    compact = record.get('format') == 'mixes' and record.get('precision') == PRECISION
    if not facts['installed']:
        return None if compact else record
    if record.get('format') != 'mixes':
        return {**record, 'context': facts['context']} if facts['context'] is not None else record
    if facts['context'] is None:
        return None if compact else record
    result = []
    for option in record['options']:
        row = facts['options'].get(str(option['id']))
        if row is None:
            raise ValueError('Prepared mix menu is incomplete')
        result.append({key: option[key] for key in ('id', 'position', 'score', 'assignment') if key in option}
                      | {'descriptor_id': row['descriptor_id'], 'genres_complete': bool(row['genres_complete']),
                         'genre_origin': row['origin']})
    return {**record, 'schema': 2, 'precision': PRECISION, 'context': facts['context'],
            'context_at': facts['context_at'], 'options': result}
