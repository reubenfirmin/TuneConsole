"""Shared catalog input contract for development training and model inference.

Read selected metadata for the exact video/recording. No annotation, prediction,
artist-level inference or prepared recommendation is evidence on this path.
Provider values are retained at their native scale; fitting applies the same
normalization and regularization as it does to captured audio features.
"""
import json
import math

from yt_playlist.util import genre_catalog

VERSION = 'catalog-audio-2/subgenres-only-1/' + genre_catalog.REVISION
NUMERIC = ('bpm', 'energy', 'danceability', 'mood_happy', 'mood_sad', 'mood_relaxed',
           'mood_acoustic', 'instrumental', 'loudness', 'dynamic_complexity',
           'mood_aggressive', 'mood_party', 'year')
SELECTED = NUMERIC[:10] + ('music_key', 'music_scale', 'genre', 'mb_year')
KEYS = ('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B')
SCALES = ('major', 'minor')
# Broad families and umbrella names have no predictor, including no presence
# flag. Exact subgenres do not activate their ancestors. This policy is shared
# by development fitting and runtime inference, not inferred from human labels.
BROAD_GENRES = frozenset(genre_catalog.normalize(genre_catalog.canonical_name(name)) for name in (
    *genre_catalog.family_labels(), *genre_catalog.family_labels().values(),
    'Electronic', 'Electronica', 'Electro', 'Dance', 'EDM', 'Dance and Electronica',
    'Rock', 'Indie', 'Alternative', 'Folk', 'Country', 'Soul', 'Funk', 'R&B', 'Hip Hop',
    'Drum & Bass', 'Garage', 'Bass', 'World', 'Latin', 'Instrumental', 'Acoustic',
    'Reggae', 'Christian', 'Holiday',
))
SUBGENRE_PREFIX = 'base.subgenre.'
FEATURES = (tuple('base.' + key for key in NUMERIC)
            + tuple('base.present.' + key for key in NUMERIC)
            + tuple('base.key.' + key for key in KEYS)
            + ('base.present.music_key',)
            + tuple('base.scale.' + key for key in SCALES)
            + ('base.present.music_scale',))


def subgenre(metadata=None):
    """One known specific genre identity; broad/unknown/missing all have no signal."""
    name = (metadata or {}).get('genre')
    if not isinstance(name, str):
        return None
    label = genre_catalog.catalog()[1].get(genre_catalog.normalize(name))
    return label[0] if label and label[0] not in BROAD_GENRES else None


def vocabulary(feature_names):
    """Only the explicit subgenres exported by this artifact, never their parents."""
    return tuple(name[len(SUBGENRE_PREFIX):] for name in feature_names if name.startswith(SUBGENRE_PREFIX))


def read(conn, video_id):
    """Bounded exact-ID reads, shared by the read-only trainer and the repository."""
    row = conn.execute('SELECT id,identity_key,recording_id,' + ','.join(SELECTED)
                       + ' FROM catalog_tracks WHERE video_id=?', (video_id,)).fetchone()
    if row is None:
        return {}
    result = dict(zip(('entry_id', 'identity_key', 'recording_id', *SELECTED), row, strict=True))
    # These native classifiers are retained in provider observations, but are
    # absent from the legacy selected columns. Never substitute genre priors.
    fields = ('mood_aggressive', 'mood_party')
    choices = {field: json.loads(value) for field, value in conn.execute(
        "SELECT field,value_json FROM entry_field_choices WHERE entry_id=? "
        "AND field IN ('mood_aggressive','mood_party')", (result['entry_id'],))}
    observations = {field: set() for field in fields}
    for raw, in conn.execute(
            'SELECT s.payload FROM rec_musical_links l JOIN rec_musical_sources s ON s.source_key=l.source_key '
            "WHERE l.recording_id=? AND s.scope='recording' "
            'AND length(CAST(s.payload AS BLOB))<=262144 ORDER BY s.source_key LIMIT 64',
            (result['recording_id'],)):
        payload = json.loads(raw)
        for field in fields:
            value = (payload.get(field) or {}).get('value')
            if type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1:
                observations[field].add(value)
    for field, values in observations.items():
        result[field] = choices.get(field) if field in choices else (next(iter(values)) if len(values) == 1 else None)
    return result


def vector(metadata=None, *, subgenres=()):
    """Unknown numeric values stay missing, including when a whole source is absent.

    The trainer imputes using training-fold means, and the runtime uses those
    exported means. Presence flags distinguish missing data from a measured zero.
    No source-confidence multiplier discounts catalog features.
    """
    metadata = metadata or {}
    values = []
    for field in NUMERIC:
        value = metadata.get(field)
        if field == 'year':
            raw = str(metadata.get('mb_year') or '')[:4]
            value = int(raw) if len(raw) == 4 and raw.isdigit() else None
        if type(value) not in (int, float) or not math.isfinite(value):
            value = None
        elif field.startswith('mood_') or field in ('energy', 'danceability', 'instrumental'):
            value = value if 0 <= value <= 1 else None
        elif field == 'bpm':
            value = value if 30 <= value <= 300 else None
        values.append(value)
    values.extend(float(value is not None) for value in values[:len(NUMERIC)])
    key = str(metadata.get('music_key') or '').strip().upper()
    key = {'DB': 'C#', 'EB': 'D#', 'GB': 'F#', 'AB': 'G#', 'BB': 'A#'}.get(key, key)
    values.extend(float(key == name) for name in KEYS)
    values.append(float(key in KEYS))
    scale = str(metadata.get('music_scale') or '').strip().lower()
    values.extend(float(scale == name) for name in SCALES)
    values.append(float(scale in SCALES))
    specific = subgenre(metadata)
    values.extend(float(specific == name) for name in subgenres)
    return values
