from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.genres import Genres
from yt_playlist.controllers.recommendation_eligibility import RecommendationEligibility
from yt_playlist.controllers.recommendation_query import RecommendationQuery
from yt_playlist.controllers.artists import Artists
from yt_playlist.repos.rec_query import RecQueryRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def query():
    return RecommendationQuery(
        create_autospec(RecQueryRepo, instance=True, spec_set=True),
        create_autospec(Genres, instance=True, spec_set=True),
        create_autospec(RecommendationEligibility, instance=True, spec_set=True),
        create_autospec(Artists, instance=True, spec_set=True))


def test_artist_genres_choose_dominant_and_preserve_ambiguous_identities(query):
    query.repository.artist_genre_counts.return_value = [
        {'artist_id': 1, 'artist': 'Aphex Twin', 'genre': 'Ambient', 'c': 1},
        {'artist_id': 1, 'artist': 'Aphex Twin', 'genre': 'IDM', 'c': 2},
        {'artist_id': 2, 'artist': 'Same Name', 'genre': 'Jazz', 'c': 3},
        {'artist_id': 3, 'artist': 'Same Name', 'genre': 'Techno', 'c': 4}]
    query.artists.resolve.side_effect = lambda name: 1 if name == 'Aphex Twin' else None
    assert query.artist_genres() == {'Aphex Twin': 'IDM', '@artist:2': 'Jazz', '@artist:3': 'Techno'}
    assert query.artist_genres(canonical=True) == {'@artist:1': 'IDM', '@artist:2': 'Jazz', '@artist:3': 'Techno'}


def test_artist_fallback_uses_dominant_genre_and_earliest_valid_year(query):
    rows = [{'genre': 'Jazz', 'mb_year': '2003-01-01'}, {'genre': 'Techno', 'mb_year': 'bad'},
            {'genre': 'Techno', 'mb_year': '1991'}, {'genre': '', 'mb_year': None}]
    query.artists.resolve_many.return_value = {'Artist': 7, 'No Tags': 8, 'Missing': None}
    query.repository.artist_metadata_for.return_value = {7: rows, 8: []}
    assert query.artist_genre_years_for(['Artist', 'No Tags', 'Missing'], per_artist=12) == {
        'Artist': {'genre': 'Techno', 'year': 1991}, 'No Tags': {'genre': '', 'year': None}}
    query.artists.resolve_many.assert_called_once_with(['Artist', 'No Tags', 'Missing'], exact=True)
    query.repository.artist_metadata_for.assert_called_once_with({7, 8}, per_artist=12)
    query.repository.artist_metadata.return_value = [
        *[{'artist': 'Artist', **row} for row in rows], {'artist': 'Unknown', 'genre': '', 'mb_year': 'bad'}]
    assert query.artist_genre_years() == {'Artist': {'genre': 'Techno', 'year': 1991}}


def test_era_and_genre_weights_include_owned_unplayed_tracks(query):
    query.repository.year_play_weights.return_value = [
        {'y': '1991', 'w': 5}, {'y': '1999-12-31', 'w': 1}, {'y': '2003', 'w': 3}, {'y': 'bad', 'w': 8}]
    assert query.era_play_distribution() == {'1990': 6, '2000': 3}
    query.repository.year_play_weights.assert_called_once_with(owned_weight=1)
    query.repository.genre_play_distribution.return_value = {'Jazz': 1}
    assert query.genre_play_distribution() == {'Jazz': 1}
    query.repository.genre_play_distribution.assert_called_once_with(owned_weight=1)


def test_facets_deduplicate_genre_and_year_support_independently(query):
    query.repository.tagged_facets.return_value = [
        {'identity_key': 'a', 'genre': 'Jazz', 'mb_year': '1991'},
        {'identity_key': 'a', 'genre': 'Jazz', 'mb_year': '1992'},
        {'identity_key': 'a', 'genre': 'House', 'mb_year': '1991'},
        {'identity_key': 'b', 'genre': 'Jazz', 'mb_year': ''},
        {'identity_key': 'c', 'genre': '', 'mb_year': '2003'}]
    assert query.play_weighted_facets({'a': 2}) == {'genres': {'House': 3, 'Jazz': 4},
                                                  'eras': {'1990': 6, '2000': 1}}


@pytest.fixture
def genre_query(query):
    query.repository.library_genre_counts.return_value = [
        {'genre': 'goa trance', 'n': 1}, {'genre': 'psytrance', 'n': 2}, {'genre': 'acid techno', 'n': 3}]
    axes = {'goa trance': ('trance', 'goa'), 'psytrance': ('trance', 'psy'),
            'acid techno': ('techno', 'acid'), 'Goa Alias': ('trance', 'goa'),
            'trance': ('trance', None), 'techno': ('techno', None)}
    query.genres.feature_axes.side_effect = lambda labels: {label: axes.get(label, ('other', None))
                                                          for label in labels if label}
    songs = {'goa trance': {'goa'}, 'psytrance': {'psy'}, 'acid techno': {'tek'}}
    query.repository.keys_for_genres.side_effect = lambda labels: set().union(*(songs[label] for label in labels))
    return query


def test_library_genre_options_combine_families_and_sort_ties(genre_query):
    assert genre_query.library_genre_families() == [{'family': 'techno', 'n': 3}, {'family': 'trance', 'n': 3}]
    assert genre_query.library_genres() == [
        {'genre': 'acid techno', 'family': 'techno', 'n': 3},
        {'genre': 'psytrance', 'family': 'trance', 'n': 2},
        {'genre': 'goa trance', 'family': 'trance', 'n': 1}]


@pytest.mark.parametrize('tokens,expected', [([], set()), ([' '], set()),
    (['goa trance'], {'goa'}), (['trance'], {'goa', 'psy'}), (['Goa Alias'], {'goa'}),
    ([' GOA TRANCE ', 'techno'], {'goa', 'tek'})])
def test_genre_selection_resolves_raw_labels_aliases_and_families(genre_query, tokens, expected):
    assert genre_query.keys_in_genre_selection(iter(tokens)) == expected
    if not expected:
        genre_query.repository.keys_for_genres.assert_not_called()


@pytest.mark.parametrize('metadata', [[], [{'genre': 'Jazz'}], [{'mb_year': '1997'}],
    [{'genre': 'Jazz'}, {'mb_year': '1997'}, {'genre': 'House', 'mb_year': '2003'},
     {'genre': 'Jazz', 'mb_year': '1998'}, {'mb_year': 'unknown'}, {}]])
def test_ordering_facets_preserve_independent_genre_and_year_support(query, metadata):
    rows = [{'genre': '', 'mb_year': '', **row} for row in metadata]
    query.repository.facet_values.return_value = rows
    query.genres.feature_axes.side_effect = lambda labels: {label: (label.lower(), label) for label in labels}
    genres, eras = query.recipe_facet_keys()
    assert genres == {row['genre'].lower(): 1 for row in rows if row['genre']}
    assert eras == {str(int(row['mb_year']) // 10 * 10): 1 for row in rows if row['mb_year'].isdigit()}


def test_catalog_queries_receive_resolved_duration(query):
    query.eligibility.maximum_duration.return_value = 600
    query.repository.library_songs.return_value = [{'key': 'song'}]
    assert query.library_songs() == [{'key': 'song'}]
    query.repository.library_songs.assert_called_once_with(maximum_duration=600)


def test_library_artist_names_are_composed_after_the_database_read(query):
    query.repository.library_artist_ids.return_value = [7, 8, 9]
    query.artists.public_keys.return_value = {7: 'unique', 8: '@artist:8', 9: '@artist:9'}
    assert query.library_artists() == {'unique', '@artist:8', '@artist:9'}
    query.artists.public_keys.assert_called_once_with([7, 8, 9])


def test_artist_track_query_passes_only_resolved_identities_to_sql(query):
    query.artists.resolve_many.return_value = {'Alias': 7, 'Artist': 7, 'Ambiguous': None}
    query.repository.tracks_by_artists.return_value = [{'key': 'song'}]
    assert query.tracks_by_artists(['Alias', 'Artist', 'Ambiguous']) == [{'key': 'song'}]
    query.repository.tracks_by_artists.assert_called_once_with({7})
