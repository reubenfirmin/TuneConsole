"""Report math consumes captured taxonomy; static-file compatibility has one owner."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers import trend_preparation
from yt_playlist.core import fao
from yt_playlist.library.trend_composition import capture_family_adjacency
from yt_playlist.rec import recap_metrics, recap_story, trend_statistics as stats
from yt_playlist.util import genre_catalog, genre_family_data, genre_map

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def detached(monkeypatch):
    genre_catalog.catalog.cache_clear()
    genre_family_data.load.cache_clear()
    monkeypatch.setattr(fao, 'read', Mock(side_effect=AssertionError('Unexpected report file read')))


def test_month_diversity_uses_supplied_family_distances_in_both_directions(detached):
    axes = {'House': ('house', None), 'Ambient': ('ambient', None)}
    rows = stats.compute_months([(1, 'one', 1), (1, 'two', 1)],
        {'one': ('A', 'House'), 'two': ('B', 'Ambient')}, {}, genre_axes=axes,
        family_adjacency={'ambient': {'house': .4}})
    assert rows[0]['families'] == {'house': 1, 'ambient': 1}
    assert rows[0]['diversity'] == pytest.approx(.2)
    assert stats.family_distance('house', 'house', {}) == 0
    assert stats.family_distance('house', 'unknown', {}) == 1
    assert stats.family_distance('house', 'ambient', {'house': {'ambient': 0}}) == 0


def test_energy_uses_explicit_family_priors_unknown_default_and_acoustic_blend(detached):
    meta = {'one': ('A', 'House'), 'two': ('B', 'Unknown')}
    axes = {'House': ('techno', None), 'Unknown': ('other:unknown', None)}
    plays = {'one': 1, 'two': 1}
    value, covered, total = recap_story.energy_axis(plays, {}, meta, genre_axes=axes)
    assert value == pytest.approx(.65) and (covered, total) == (0, 2)
    value, covered, total = recap_story.energy_axis(plays, {'one': {'energy': .2}}, meta, genre_axes=axes)
    assert value == pytest.approx(.425) and (covered, total) == (1, 2)
    assert recap_story.energy_axis({}, {}, {}, genre_axes={}) == (.5, 0, 0)


def test_profile_uses_supplied_family_even_when_null_and_requires_uncaptured_metadata(detached):
    since, until = recap_metrics.month_bounds('2026-10')
    event = {'account_id': 1, 'identity_key': 'one', 'played_at': since + 3600, 'source': 'takeout'}
    spans = {(1, 'takeout'): (since - 1, until)}
    card = {'genre': 'House', 'family': 'techno'}
    def profile():
        return recap_metrics.profile('2026-10', [event], {'one': card}, spans, [])
    assert profile().families == {'techno': 1}
    card['family'] = None
    assert profile().families == {}
    del card['family']
    with pytest.raises(ValueError, match='supply the genre family'):
        profile()


def test_preparation_passes_captured_family_distances_to_actual_month_calculation(detached, monkeypatch):
    trends = Mock(spec_set=['has_play_ledger', 'ledger_day_counts', 'track_meta',
                            'first_play_map', 'catalog_artists'])
    trends.has_play_ledger.return_value = True
    trends.ledger_day_counts.return_value = [(1, 'one', 1), (1, 'two', 1)]
    trends.track_meta.return_value = {'one': ('A', 'House'), 'two': ('B', 'Ambient')}
    trends.first_play_map.return_value = {}
    trends.catalog_artists.return_value = set()
    genres = Mock(spec_set=['feature_axes'])
    genres.feature_axes.return_value = {'House': ('house', None), 'Ambient': ('ambient', None)}
    source = NS(trends=trends, genres=genres, family_adjacency={'house': {'ambient': .6}})
    monkeypatch.setattr(trend_preparation, 'assemble', lambda s, now, counts, meta, months, *, artist_first: months)
    assert trend_preparation.calculate(source, 1000)[0]['diversity'] == pytest.approx(.3)


def test_captured_adjacency_and_energy_preserve_installed_taxonomy_without_later_file_reads(monkeypatch):
    adjacency = capture_family_adjacency()
    families = [*genre_map.all_families(), 'other:unmapped']
    expected = {(a, b): genre_map.family_distance(a, b) for a in families for b in families}
    _, labels = genre_catalog.catalog()
    axes = {label: (genre_map.family(label), None) for label in labels}
    energies = {label: recap_story._FAMILY_ENERGY.get(family, .5)
                for label, (family, _) in axes.items()}
    monkeypatch.setattr(fao, 'read', Mock(side_effect=AssertionError('Unexpected report file read')))
    for (a, b), distance in expected.items():
        assert stats.family_distance(a, b, adjacency) == distance
    for label, energy in energies.items():
        assert recap_story.energy_axis({'one': 1}, {}, {'one': ('A', label)},
                                      genre_axes=axes) == (energy, 0, 1)
    with pytest.raises(TypeError):
        adjacency['new'] = {}
    family = next(iter(adjacency))
    with pytest.raises(TypeError):
        adjacency[family]['new'] = .4
