"""The finite typed catalog metadata vocabulary and its evidence value contract.

Provider tag bags and native classifier documents remain opaque delivery
evidence for their existing musical-profile consumers. They are not new catalog
attributes and cannot be admitted through this vocabulary.
"""
from dataclasses import dataclass


RECORDING_FIELDS = (
    'genre', 'bpm', 'energy', 'danceability', 'mb_recording_id', 'music_key', 'music_scale',
    'mood_happy', 'mood_sad', 'mood_relaxed', 'mood_acoustic', 'instrumental',
)
ITEM_FIELDS = (
    'title', 'artist', 'orig_title', 'orig_artist', 'album', 'duration_s', 'available', 'video_type',
    'artist_browse_id', 'album_browse_id', 'thumbnail', 'mb_year', 'loudness', 'dynamic_complexity',
    'popularity', 'gain', 'label',
)
PROBABILITIES = ('energy', 'danceability', 'mood_happy', 'mood_sad', 'mood_relaxed', 'mood_acoustic', 'instrumental')
INTEGER_FIELDS = ('duration_s', 'available', 'popularity')
NUMBER_FIELDS = (*PROBABILITIES, *INTEGER_FIELDS, 'bpm', 'loudness', 'dynamic_complexity', 'gain')
TEXT_FIELDS = tuple(field for field in (*RECORDING_FIELDS, *ITEM_FIELDS) if field not in NUMBER_FIELDS)
UNITS = {
    'bpm': ('bpm',), 'duration_s': ('seconds',), 'available': ('boolean',), 'popularity': ('provider_score',),
    'gain': ('decibels',), 'loudness': ('essentia.average_loudness',),
    'dynamic_complexity': ('essentia.dynamic_complexity',),
    **{field: ('classifier_probability', 'fraction') for field in PROBABILITIES},
    'energy': ('legacy_energy_0_1', 'fraction'),
}
DEFAULT_UNIT = {field: units[-1] for field, units in UNITS.items()}
MAX_TEXT_BYTES = 64 * 1024


@dataclass(frozen=True)
class Value:
    kind: str
    text: str | None = None
    number: int | float | None = None
    genre_key: str | None = None
    unit: str | None = None
    resolution_reason: str | None = None

    def selected(self):
        if self.kind in ('unresolved', 'withdrawn'):
            raise ValueError('Unresolved evidence or a withdrawn choice cannot supply a typed value')
        return self.number if self.number is not None else self.text


def scope(field):
    if field not in (*RECORDING_FIELDS, *ITEM_FIELDS):
        raise ValueError(f'Unsupported catalog metadata field: {field}')
    return 'recording' if field in RECORDING_FIELDS else 'release' if field in ('mb_year', 'label') else 'item'


def prepare(conn, field, value, *, unit=None):
    prepared = prepare_value(field, value, unit=unit)
    if field == 'genre' and prepared.kind == 'value':
        from .genre_identity import resolve
        genre, reason = resolve(conn, value)
        return Value(prepared.kind, text=value, genre_key=genre, resolution_reason=reason)
    return prepared


def prepare_value(field, value, *, unit=None, genre=None, resolution_reason=None):
    """Validate new evidence without coercing strings, booleans or missing units."""
    scope(field)
    if value is None:
        if unit is not None:
            raise ValueError('Null metadata has no numeric unit')
        return Value('null')
    if isinstance(value, str) and value == '':
        if unit is not None:
            raise ValueError('Blank metadata has no numeric unit')
        # Blank and null have different choice semantics, including on a
        # nominally numeric field. A blank is never silently changed to zero.
        return Value('blank', text='')
    if field in TEXT_FIELDS:
        if not isinstance(value, str) or len(value.encode()) > MAX_TEXT_BYTES or unit is not None:
            raise ValueError('Text metadata requires bounded text without a numeric unit')
        return Value('value', text=value, genre_key=genre if field == 'genre' else None,
                     resolution_reason=resolution_reason if field == 'genre' else None)
    if (type(value) not in (int, float)
            or not -1.7976931348623157e308 <= value <= 1.7976931348623157e308
            or unit not in UNITS[field]):
        raise ValueError('Numeric metadata requires a finite number and its supported unit')
    if field in INTEGER_FIELDS and (type(value) is not int or not -(1 << 63) <= value < 1 << 63):
        raise ValueError('Integral metadata requires an exact SQLite integer')
    if ((field in PROBABILITIES and not 0 <= value <= 1)
            or field == 'available' and value not in (0, 1)
            or field == 'duration_s' and value < 0
            or field == 'bpm' and value <= 0):
        raise ValueError('Metadata number is outside its field domain')
    return Value('value', number=value, unit=unit)
