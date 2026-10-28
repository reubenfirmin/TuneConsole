"""Pure, pool-independent musical descriptions; engineering priors are uncalibrated.

Source probability, lookup evidence and our bounded reliability weight remain
separate. These descriptions never learn from intent, taste, or their own output.
Listening validation and calibration belong to the benchmark, not these fixtures.
"""
import functools
import hashlib
import json
import math

from yt_playlist.util import genre_catalog

VERSION = 'musical-profile-2/genre-priors-1/' + genre_catalog.REVISION
DIMENSIONS = ('intensity', 'tempo', 'groove', 'instrumental', 'acoustic', 'relaxed',
              'happiness', 'sadness', 'aggression', 'density')

# Initial engineering priors, deliberately sparse and weak. No genre implies
# exact tempo, instrumentation, vocal presence, or emotional valence.
_GENRES = {
    'Ambient': {'intensity': .15, 'density': .25, 'relaxed': .8},
    'Downtempo': {'intensity': .25, 'groove': .55},
    'Chillout': {'intensity': .2, 'relaxed': .8},
    'Metal': {'intensity': .8, 'density': .8},
    'Doom Metal': {'intensity': .85, 'density': .9},
    'Death Metal': {'intensity': .95, 'density': .9},
    'Thrash Metal': {'intensity': .95, 'density': .85},
    'Hardcore': {'intensity': .9, 'density': .8},
    'Noise': {'intensity': .85, 'density': .9},
    'Noise Rock': {'intensity': .85, 'density': .85},
    'Industrial': {'intensity': .8, 'density': .8},
    'Techno': {'intensity': .7, 'groove': .8},
    'House': {'intensity': .6, 'groove': .85},
    'Deep House': {'intensity': .45, 'groove': .8},
    'Drum & Bass': {'intensity': .8, 'groove': .8},
    'Gabber': {'intensity': .95, 'density': .85},
    'Disco': {'intensity': .6, 'groove': .9},
    'Funk': {'groove': .85},
    'Acoustic': {'intensity': .3},
}
_TRAITS = {
    'aggressive': {'aggression': .9, 'intensity': .85},
    'energetic': {'intensity': .85}, 'mellow': {'intensity': .2, 'relaxed': .8},
    'relaxing': {'relaxed': .85, 'intensity': .2},
    'dense': {'density': .85}, 'sparse': {'density': .15},
    'danceable': {'groove': .85}, 'instrumental': {'instrumental': .9},
    'acoustic': {'acoustic': .85}, 'happy': {'happiness': .85}, 'sad': {'sadness': .85},
}
_RAW = {'danceability': 'groove', 'instrumental': 'instrumental', 'mood_acoustic': 'acoustic',
        'mood_relaxed': 'relaxed', 'mood_happy': 'happiness', 'mood_sad': 'sadness',
        'mood_aggressive': 'aggression'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def number(value, *, low=0, high=1):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and low <= value <= high else None


@functools.lru_cache(maxsize=2048)
def genre_profile(name):
    """Canonical IDs, child overrides, at most two explicit parent edges."""
    _records, labels = genre_catalog.catalog()
    label = labels.get(genre_catalog.normalize(name))
    if not label:
        return {}
    key = label[0]
    priors = {labels[genre_catalog.normalize(n)][0]: values for n, values in _GENRES.items()
              if genre_catalog.normalize(n) in labels}
    queue, seen, anchors = [(key, 0)], set(), {}
    while queue:
        node, depth = queue.pop(0)
        if node in seen or depth > 2:
            continue
        seen.add(node)
        for dimension, value in priors.get(node, {}).items():
            nearest = anchors.setdefault(dimension, [])
            if not nearest or nearest[0]['depth'] == depth:
                nearest.append({'value': value, 'support': .2 * .65 ** depth,
                    'kind': 'curated' if depth == 0 else 'inherited', 'genre_id': key,
                    'anchor_id': node, 'depth': depth, 'variation': .3,
                    'provenance': 'engineering-priors-1; listening calibration pending'})
        queue.extend((parent, depth + 1) for parent in sorted(genre_catalog._parents().get(node, ())))
    return {name: combine(values) for name, values in anchors.items()}


def _match(provenance):
    match = provenance.get('match_evidence') or {}
    method = match.get('method')
    if method == 'name_query':
        return .7 if match.get('title_artist_match') is True else 0
    return {'title_artist_match': .8, 'matched_release_or_tracklist': .7,
            'supplied_recording_mbid': .6, 'musicbrainz_lookup': .55}.get(method, .35)


def source_profile(source):
    """One provider/entity's native observations; never aggregate prior resolutions."""
    canonical(source)  # The retention boundary also rejects nonfinite or non-JSON evidence.
    provider, scope, entity_id = source['provider'], source['scope'], source['entity_id']
    observations = source['observations']
    candidates = {name: [] for name in DIMENSIONS}
    def add(name, value, support, field, kind, extra=None):
        candidates[name].append({'value': value, 'support': support, 'kind': kind,
            'source': [provider, scope, entity_id, field], **(extra or {})})
    tags = observations.get('tags')
    if tags:
        provenance = tags['provenance']
        match_weight = _match(provenance)
        names = list(dict.fromkeys(tag['name'] for tag in tags['value']
                                  if isinstance(tag, dict) and isinstance(tag.get('name'), str)))
        # Keep canonical distinct styles; an alias or a broad parent adds no corroboration.
        styles = list(dict.fromkeys(genre_catalog.canonical_name(name) for name in names))
        styles = [name for name in styles if not any(genre_catalog.is_subgenre(other, name)
                                                    for other in styles)]
        scope_cap = {'recording': .25, 'release': .16, 'artist': .1}.get(scope, 0)
        for name in styles:
            for dimension, prior in genre_profile(name).items():
                add(dimension, prior['value'], min(scope_cap, prior['support']) * match_weight,
                    'tags', 'inherited', {'genre': prior})
        if scope == 'recording':
            for tag in names:
                for dimension, value in _TRAITS.get(genre_catalog.normalize(tag), {}).items():
                    add(dimension, value, .25 * match_weight, 'tags', 'reported')
    if scope == 'recording':
        for field, dimension in _RAW.items():
            observation = observations.get(field)
            if observation and (value := number(observation['value'])) is not None:
                add(dimension, value, .75 * _match(observation['provenance']), field,
                    observation['provenance'].get('kind', 'prediction'), {'native': True})
        bpm = observations.get('bpm')
        if bpm and (value := number(bpm['value'], low=30, high=300)) is not None:
            add('tempo', (value - 30) / 270, .65 * _match(bpm['provenance']), 'bpm',
                bpm['provenance'].get('kind', 'prediction'),
                {'native': True, 'bpm': value, 'alternatives_bpm': [value / 2, value, value * 2]})
        # Legacy derived energy and native loudness/dynamics remain retained in
        # observations. They are not independent votes or normalized gain here.
    dimensions = {}
    for name, values in candidates.items():
        if not values:
            continue
        # Raw recording classifiers take precedence over descriptive tag priors.
        direct = [value for value in values if value.get('native')]
        dimensions[name] = combine(direct or values)
    return {'schema': 1, 'version': VERSION, 'provider': provider, 'scope': scope,
            'entity_id': entity_id, 'dimensions': dimensions, 'observations': observations,
            'reliability': {'status': 'uncalibrated', 'policy': 'bounded-engineering-weights-1'}}


def combine(values):
    values = [value for value in values if value['support'] > 0]
    if not values:
        return {'value': None, 'support': 0, 'evidence': []}
    weight = sum(value['support'] for value in values)
    spread = max(value['value'] for value in values) - min(value['value'] for value in values)
    return {'value': sum(value['value'] * value['support'] for value in values) / weight,
            'support': max(value['support'] for value in values) * (1 - .5 * spread),
            'conflict': spread > .35, 'evidence': values}


def _only_priors(dimension):
    """Genre inheritance cannot hide an existing recording-level selected field."""
    evidence = dimension.get('evidence') or []
    return bool(evidence) and all(
        value.get('kind') in ('curated', 'inherited') or _only_priors(value)
        for value in evidence)


def resolve(sources, *, selected=None, overrides=None, genre=None):
    """Resolve one recording without pool statistics or recursive inherited evidence."""
    selected, overrides = selected or {}, overrides or {}
    source_versions = [digest(source) for source in sources]
    observed = {field for source in sources if source['scope'] == 'recording'
                for field in source['observations']}
    if 'genre' in overrides:
        genre = overrides['genre']
        labels = genre_catalog.catalog()[1]
        revised = []
        for source in sources:
            observations = dict(source['observations'])
            if tags := observations.get('tags'):
                observations['tags'] = {**tags, 'value': [tag for tag in tags['value']
                    if genre_catalog.normalize(tag.get('name', '')) not in labels]}
            revised.append(source_profile({**source, 'observations': observations}))
        sources = revised
    dimensions = {}
    for name in DIMENSIONS:
        options = []
        for source in sources:
            dimension = source['dimensions'].get(name)
            if dimension and dimension['value'] is not None:
                options.append({**dimension, 'scope': source['scope'],
                                'source': [source['provider'], source['scope'], source['entity_id']]})
        direct = [value for value in options if value['scope'] == 'recording']
        releases = [value for value in options if value['scope'] == 'release']
        artists = [value for value in options if value['scope'] == 'artist']
        prior = genre_profile(genre).get(name) if genre else None
        chosen = direct or releases or artists or ([prior] if prior else [])
        dimensions[name] = combine(chosen)
    # Resolve correlated classifier components once, after explicit corrections.
    # Missing components contribute neutral evidence, never a fabricated zero.
    evidence, support, components = 0, 0, []
    for field, weight, invert in (('mood_aggressive', .55, False), ('mood_party', .25, False),
                                  ('mood_relaxed', .2, True)):
        values = []
        if field in overrides:
            value = number(overrides[field])
            if value is not None:
                values = [{'value': value, 'support': 1, 'kind': 'explicit', 'field': field}]
        else:
            for source in sources:
                observation = source['observations'].get(field) if source['scope'] == 'recording' else None
                if observation and (value := number(observation['value'])) is not None:
                    values.append({'value': value, 'support': .75 * _match(observation['provenance']),
                                   'source': [source['provider'], field],
                                   'kind': observation['provenance'].get('kind', 'prediction')})
        component = combine(values)
        if component['value'] is not None:
            evidence += weight * ((1 - component['value'] if invert else component['value']) - .5)
            support += weight * component['support']
            components.append({'field': field, **component})
    if support:
        dimensions['intensity'] = {'value': .5 + evidence, 'support': support,
                                    'evidence': components, 'kind': 'derived'}
    for field, name in {'energy': 'intensity', 'bpm': 'tempo', 'danceability': 'groove',
                        **_RAW}.items():
        if field in overrides:
            value = number(overrides[field], low=30 if field == 'bpm' else 0,
                           high=300 if field == 'bpm' else 1)
            dimensions[name] = {'value': (value - 30) / 270 if field == 'bpm' and value is not None else value,
                                'support': 1 if value is not None else 0,
                                'evidence': [{'kind': 'explicit', 'field': field}], 'override': True}
        elif ((dimensions[name]['value'] is None or _only_priors(dimensions[name]))
              and field in selected and field not in observed
              and not (field == 'energy' and any(component in observed or component in overrides for component in
                  ('mood_aggressive', 'mood_party', 'mood_relaxed')))):
            value = number(selected[field], low=30 if field == 'bpm' else 0,
                           high=300 if field == 'bpm' else 1)
            if value is not None:
                dimensions[name] = {'value': (value - 30) / 270 if field == 'bpm' else value,
                                    'support': .35, 'evidence': [{'kind': 'legacy_selected', 'field': field}]}
    intensity = dimensions['intensity']
    if intensity['value'] is not None and not intensity.get('override'):
        intensity['value'] = .5 + (intensity['value'] - .5) * min(1, intensity['support'] / .6)
    return {'schema': 1, 'version': VERSION, 'dimensions': dimensions,
            'source_versions': source_versions,
            'reliability': {'status': 'uncalibrated', 'policy': 'bounded-engineering-weights-1'}}
