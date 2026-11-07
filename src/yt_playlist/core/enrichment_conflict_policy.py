"""Validate user selections independently of conflict persistence."""
from yt_playlist.core.enrichment_contract import FIELD_COLUMN
from yt_playlist.core.metadata_contract import ALTERNATIVE_SECONDS
from yt_playlist.core import metadata_values


def current_candidates(document, updated_at, now):
    return [candidate for candidate in document.get('candidates', [])
            if candidate.get('at', updated_at) > now - ALTERNATIVE_SECONDS]


def prepare_candidates(field, candidates):
    field = 'mb_year' if field == 'year' else field
    metadata_values.scope(field)
    if not isinstance(candidates, (list, tuple)) or len(candidates) > 64:
        raise ValueError('Metadata conflict exceeds its candidate budget')
    prepared = []
    for candidate in candidates:
        if (not isinstance(candidate, dict) or not isinstance(candidate.get('provider'), str)
                or not candidate['provider'] or 'value' not in candidate):
            raise ValueError('A metadata candidate needs its provider and offered value')
        value = metadata_values.prepare_value(field, candidate['value'],
            unit=metadata_values.DEFAULT_UNIT.get(field) if candidate['value'] not in (None, '') else None)
        prepared.append({**candidate, 'prepared': value})
    return field, prepared


def retain_candidates(previous, offered, refreshed):
    providers = {candidate['provider'] for candidate in offered}
    return [candidate for candidate in previous if refreshed is not None
            and candidate['provider'] not in refreshed and candidate['provider'] not in providers]


def refreshed_status(previous_status, previous, current):
    if previous_status == 2 or previous_status == 1 and (
            {str(candidate['value']) for candidate in current} - {str(candidate['value']) for candidate in previous}):
        return 0
    return previous_status


def agreed(previous, refreshed):
    return len(refreshed) >= 2 and {candidate['provider'] for candidate in previous} <= set(refreshed)


def selected_value(field, value):
    spec = FIELD_COLUMN.get(field)
    if spec is None:
        raise ValueError(f'not a writable enrichment field: {field!r}')
    column, cast = spec
    try:
        prepared = cast(value) if value not in (None, '') else ('' if cast is str else None)
    except (TypeError, ValueError):
        prepared = value
    return column, prepared


def offered_value(current, candidates, value):
    offered = {str(candidate['value']) for candidate in candidates}
    offered.add(str(current) if current is not None else '')
    return value in offered
