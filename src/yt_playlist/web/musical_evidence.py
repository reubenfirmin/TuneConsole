"""Readable descriptions of saved musical evidence; no inference or provider I/O."""
import math

from yt_playlist.rec import home_vibe, musical_character
from yt_playlist.rec.musical_profiles import DIMENSIONS

_LABELS = {'intensity': 'Intensity', 'tempo': 'Tempo', 'groove': 'Groove',
           'instrumental': 'Instrumental', 'acoustic': 'Acoustic', 'relaxed': 'Relaxedness',
           'happiness': 'Happy mood', 'sadness': 'Sad mood', 'aggression': 'Aggression', 'density': 'Density'}
_PROVIDERS = {'acousticbrainz': 'AcousticBrainz', 'musicbrainz': 'MusicBrainz',
              'lastfm': 'Last.fm', 'discogs': 'Discogs'}
_KINDS = {'explicit': 'Your correction', 'legacy_selected': 'Legacy metadata',
          'prediction': 'Model prediction', 'measurement': 'Measurement',
          'reported': 'Reported tags', 'curated': 'Genre hint', 'inherited': 'Inherited hint'}
_MATCHES = {'supplied_recording_mbid': 'Recording ID lookup',
            'title_artist_match': 'Title and artist lookup', 'musicbrainz_lookup': 'MusicBrainz lookup',
            'matched_release_or_tracklist': 'Release or tracklist lookup', 'name_query': 'Name lookup'}
_VIBE_SOURCES = {'audio_measurement': 'Audio model estimate',
                 'provider_measurement': 'Provider attribute estimate',
                 'artist_genre': 'Artist and genre estimate', 'artist': 'Artist estimate',
                 'genre': 'Genre estimate', 'heuristic': 'Attribute estimate'}


def _basis(dimension):
    stack, kinds, sources = [dimension], set(), set()
    for _ in range(512):
        if not stack:
            break
        node = stack.pop()
        if node.get('kind') in _KINDS:
            kinds.add(node['kind'])
        source = node.get('source') or []
        if source:
            scope = source[1] if len(source) >= 3 else 'recording'
            sources.add(f"{_PROVIDERS.get(source[0], source[0])} ({scope})")
        stack.extend(node.get('evidence') or [])
    labels = [_KINDS[kind] for kind in _KINDS if kind in kinds]
    return ' · '.join(labels + sorted(sources)) or 'No retained source detail'


def describe(profile, sources=(), *, truncated=False):
    """Keep probabilities, engineering support and recording matches distinct."""
    rows = []
    for name in DIMENSIONS:
        dimension = (profile or {}).get('dimensions', {}).get(name) or {}
        value = dimension.get('value')
        if value is None:
            description = 'Unavailable after corrections' if dimension.get('override') else 'Unknown'
        elif name == 'tempo':
            description = f'{30 + 270 * value:.0f} BPM (estimate)'
        else:
            description = 'Low' if value < .35 else 'High' if value > .65 else 'Midrange'
        rows.append({'name': name, 'label': _LABELS[name], 'description': description,
                     'known': value is not None,
                     'basis': _basis(dimension) if value is not None or dimension.get('override') else '',
                     'limited': value is not None and dimension.get('support', 0) < .4,
                     'conflict': bool(dimension.get('conflict'))})
    retained = []
    for source in sources:
        observations = []
        for field, observation in sorted(source.get('observations', {}).items()):
            provenance = observation.get('provenance') or {}
            value = observation.get('value')
            # Tag lists are already represented by their inherited signal rows.
            if field == 'tags' and isinstance(value, list):
                value = ', '.join(tag['name'] for tag in value[:12] if isinstance(tag, dict) and 'name' in tag)
            observations.append({'field': field.replace('_', ' '), 'value': str(value)[:500],
                'kind': _KINDS.get(provenance.get('kind'), 'Source observation'),
                'unit': provenance.get('unit'),
                'model': provenance.get('model_version'), 'provider_version': provenance.get('provider_version'),
                'match': _MATCHES.get((provenance.get('match_evidence') or {}).get('method'), 'Match method unknown')})
        retained.append({'provider': _PROVIDERS.get(source['provider'], source['provider']),
                         'scope': source['scope'], 'observations': observations})
    vibes = (profile or {}).get('vibes') or {}
    fits = vibes.get('fits') or {}
    vibe_rows = []
    for name, preset in home_vibe.PRESETS.items():
        fit = fits.get(name) or {}
        score, source = fit.get('score'), fit.get('source')
        known = type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1
        # A neutral fallback with zero support is still unknown evidence.
        attributes_known = any(musical_character.dimension({'musical': profile}, dimension)[0] is not None
                               for dimension in preset['weights'])
        supported = known and source in _VIBE_SOURCES and (
            fit.get('support', 0) > 0 or (source == 'heuristic' and attributes_known))
        vibe_rows.append({'name': name, 'label': preset['label'],
            'description': f'{score * 100:.0f}/100' if supported else 'Unknown',
            'basis': _VIBE_SOURCES.get(source, 'Source unknown') if known else '',
            'recordings': fit.get('recordings') if source in ('artist_genre', 'artist', 'genre') else None})
    return {'rows': rows, 'known': sum(row['known'] for row in rows), 'total': len(rows),
            'vibes': vibe_rows, 'vibe_version': vibes.get('version'), 'vibe_inputs_version': vibes.get('inputs_version'),
            'sources': retained, 'truncated': truncated or bool((profile or {}).get('sources_truncated'))}
