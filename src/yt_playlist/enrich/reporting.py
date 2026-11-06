"""Detached enrichment provenance, conflict choices and progress charts."""
from yt_playlist.core.enrichment_contract import FIELD_COLUMN
from yt_playlist.core.metadata_contract import ALTERNATIVE_SECONDS


def current_candidates(document, updated_at, now):
    return [candidate for candidate in document.get('candidates', [])
            if candidate.get('at', updated_at) > now - ALTERNATIVE_SECONDS]


def genre_provenance(track, candidates, conflicts):
    candidates = list(candidates)
    seen = {(candidate['provider'], candidate['value']) for candidate in candidates}
    for candidate in conflicts:
        pair = str(candidate.get('provider') or 'unknown'), str(candidate.get('value') or '')
        if pair not in seen:
            candidates.append({'provider': pair[0], 'value': pair[1]})
            seen.add(pair)
    options, by_value = [], {}
    for candidate in candidates:
        value = candidate['value']
        if not value:
            continue
        if value not in by_value:
            by_value[value] = {'value': value, 'providers': []}
            options.append(by_value[value])
        providers = by_value[value]['providers']
        if candidate['provider'] not in providers:
            providers.append(candidate['provider'])
    return {**{key: track[key] for key in ('video_id', 'title', 'artist')},
            'track_id': track['id'], 'current': track['genre'], 'candidates': candidates, 'options': options}


def review_conflict(conflict, *, current, selected_source, findings, overridden):
    spec = FIELD_COLUMN.get(conflict['field'])
    cast = spec[1] if spec else str

    def key(value):
        if value is None or value == '':
            return None
        try:
            value = cast(value)
        except (TypeError, ValueError):
            pass
        return value.strip().casefold() if isinstance(value, str) else value

    current_key = key(current)
    options = [{'value': str(current) if current is not None else '',
                'label': current if current not in (None, '') else 'Not set',
                'current': True, 'providers': [], 'source_note': ''}]
    by_value = {current_key: options[0]}
    for candidate in conflict['candidates']:
        value = candidate['value']
        option = by_value.get(key(value))
        if option is None:
            option = {'value': str(value) if value is not None else '',
                      'label': value if value not in (None, '') else 'Not set', 'current': False,
                      'providers': [], 'source_note': ''}
            by_value[key(value)] = option
            options.append(option)
        if candidate['provider'] not in option['providers']:
            option['providers'].append(candidate['provider'])
    if not options[0]['providers'] and current_key is not None:
        if selected_source:
            options[0]['providers'].append(selected_source)
        for finding in findings:
            if key(finding['value']) == current_key and finding['provider'] not in options[0]['providers']:
                options[0]['providers'].append(finding['provider'])
    if overridden:
        options[0]['source_note'] = 'Your choice'
    elif not options[0]['providers']:
        options[0]['source_note'] = ('No saved value' if current_key is None
                                     else 'Saved locally; original source not recorded')
    for option in options:
        option['id'] = (f"enrich-choice-{conflict['track_id']}-{conflict['field']}-"
                        + option['value'].encode().hex())
    provider_order = {}
    for candidate in conflict['candidates']:
        provider_order.setdefault(key(candidate['value']), len(provider_order))
    options.sort(key=lambda option: provider_order.get(key(option['value']), -1))
    return {**conflict, 'current': current, 'can_resolve': spec is not None, 'options': options}


def processed_timeline(rows, buckets):
    if not rows:
        return []
    refreshed = sorted(row['last'] for row in rows
                       if row['last'] is not None and row['last'] > row['first'])
    lo, hi, total = rows[0]['first'], max(rows[-1]['first'], max(refreshed, default=0)), len(rows)
    if hi <= lo:
        return [{'t': hi, 'n': total, 'refresh': 0}]
    width = (hi - lo) / buckets
    out, first_count, refreshed_count = [], 0, 0
    for index in range(1, buckets + 1):
        edge = lo + width * index
        while first_count < len(rows) and rows[first_count]['first'] <= edge:
            first_count += 1
        while refreshed_count < len(refreshed) and refreshed[refreshed_count] <= edge:
            refreshed_count += 1
        out.append({'t': edge, 'n': first_count, 'refresh': refreshed_count})
    out[-1]['n'], out[-1]['refresh'] = total, len(refreshed)
    return out
