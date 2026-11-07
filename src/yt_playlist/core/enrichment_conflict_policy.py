"""Validate user selections independently of conflict persistence."""
from yt_playlist.core.enrichment_contract import FIELD_COLUMN
from yt_playlist.core.metadata_contract import ALTERNATIVE_SECONDS


def current_candidates(document, updated_at, now):
    return [candidate for candidate in document.get('candidates', [])
            if candidate.get('at', updated_at) > now - ALTERNATIVE_SECONDS]


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
