"""Typed metadata never guesses types, units, or genre identities."""
import pytest

from yt_playlist.core import metadata_values as values


@pytest.mark.parametrize('field', (*values.RECORDING_FIELDS, *values.ITEM_FIELDS))
def test_null_and_blank_are_different_for_every_supported_field(store, field):
    assert values.prepare(store.conn, field, None).kind == 'null'
    blank = values.prepare(store.conn, field, '')
    assert blank.kind == 'blank' and blank.selected() == ''


@pytest.mark.parametrize('field,value,unit', [
    ('bpm', '120', 'bpm'), ('bpm', True, 'bpm'), ('bpm', 120, None), ('bpm', 120, 'hz'), ('bpm', 0, 'bpm'),
    ('energy', 2, 'fraction'), ('energy', -.1, 'fraction'), ('energy', float('inf'), 'fraction'),
    ('energy', float('nan'), 'fraction'), ('energy', 10 ** 400, 'fraction'), ('available', 2, 'boolean'),
    ('available', True, 'boolean'), ('duration_s', -1, 'seconds'), ('duration_s', 2.0, 'seconds'),
    ('duration_s', 1 << 63, 'seconds'), ('popularity', 2.5, 'provider_score'), ('title', 1, None),
    ('title', 'Song', 'bpm'), ('tags', ['Rock'], None), ('new_attribute', 'arbitrary', None),
    ('music_key', 4, None), ('mood_party', .5, 'fraction'),
])
def test_invalid_type_unit_domain_or_attribute_is_rejected(store, field, value, unit):
    with pytest.raises(ValueError):
        values.prepare(store.conn, field, value, unit=unit)


@pytest.mark.parametrize('field,value,unit', [
    ('bpm', 120, 'bpm'), ('energy', 0, 'legacy_energy_0_1'), ('energy', 1, 'fraction'),
    ('mood_happy', .5, 'classifier_probability'), ('available', 0, 'boolean'), ('available', 1, 'boolean'),
    ('duration_s', 0, 'seconds'), ('popularity', 100, 'provider_score'), ('gain', -3.2, 'decibels'),
    ('loudness', .82, 'essentia.average_loudness'), ('dynamic_complexity', 4.2, 'essentia.dynamic_complexity'),
])
def test_supported_numbers_keep_their_values_and_units(store, field, value, unit):
    prepared = values.prepare(store.conn, field, value, unit=unit)
    assert prepared.number == value and prepared.unit == unit and prepared.selected() == value


def test_genre_is_bound_only_to_an_established_unambiguous_name_or_alias(store):
    with store.conn:
        store.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('specific','Specific','test')")
        store.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES ('alias','Alias','specific','test')")
    for text in ('specific', 'Specific', 'Alias'):
        result = values.prepare(store.conn, 'genre', text)
        assert result.genre_key == 'specific' and result.text == text and result.resolution_reason is None
    unknown = values.prepare(store.conn, 'genre', 'Unrecognized spelling')
    assert unknown.genre_key is None and unknown.resolution_reason == 'unknown_genre'
    assert unknown.selected() == 'Unrecognized spelling'


def test_unknown_or_withdrawn_evidence_cannot_be_selected():
    for kind in ('unresolved', 'withdrawn'):
        with pytest.raises(ValueError, match='cannot supply'):
            values.Value(kind).selected()
