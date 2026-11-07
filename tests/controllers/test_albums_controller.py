from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.albums import Albums
from yt_playlist.controllers.artists import Artists
from yt_playlist.controllers.genres import Genres
from yt_playlist.repos.albums import AlbumRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def albums():
    repository = create_autospec(AlbumRepo, instance=True, spec_set=True)
    artists = create_autospec(Artists, instance=True, spec_set=True)
    genres = create_autospec(Genres, instance=True, spec_set=True)
    writing = False
    @contextmanager
    def transaction():
        nonlocal writing
        writing = True
        try:
            yield
        finally:
            writing = False
    def ensure(*args, **kwargs):
        assert not writing, 'Artist writes must use their own repository unit'
        return 7
    artists.ensure_artist.side_effect = ensure
    repository.transaction.side_effect = transaction
    repository.observation_inputs.return_value = {'album': {}, 'keyed_genres': True, 'genre': None, 'credits': []}
    repository.store_metadata.return_value = 12
    return Albums(repository, artists, genres)


@pytest.mark.parametrize('previous_rank,rank,title,expected', [(3, 1, 'Partial', 'Full'),
    (1, 3, 'Full refresh', 'Full refresh'), (3, 3, '', 'Full')])
def test_metadata_authority_is_resolved_before_sql_and_empty_values_preserve_known_fields(albums, previous_rank, rank, title, expected):
    current = dict(id=12, title='Full', artist='Artist', metadata_rank=previous_rank, year='2000')
    albums.repository.observation_inputs.return_value = dict(album=current, keyed_genres=True, genre=None,
        credits=[dict(artist_id=7, credit='Artist')])
    assert albums.observe('B', title=title, rank=rank) == 12
    prepared = albums.repository.store_metadata.call_args.args[2]
    assert prepared['title'] == expected and prepared['year'] == '2000'
    assert prepared['metadata_rank'] == max(previous_rank, rank)
    albums.repository.store_credits.assert_not_called()


def test_structured_credits_use_exact_release_context_after_metadata_commit(albums):
    albums.artists.credit_subject.return_value = 3
    albums.artists.release_subject.return_value = None
    albums.observe('B', artist='Artist', credits=[{'name': 'Artist', 'id': 'channel'}], rank=3)
    albums.artists.release_subject.assert_called_once_with(12, 3)
    albums.artists.ensure_artist.assert_called_once_with('Artist', provider='youtube', provider_id='channel', previous=None)
    albums.repository.store_credits.assert_called_once_with(12, [(7, 'Artist')])


def test_low_authority_refresh_cannot_replace_existing_identified_credits(albums):
    albums.repository.observation_inputs.return_value = dict(album={'id': 12, 'metadata_rank': 3},
        keyed_genres=True, genre=None, credits=[dict(artist_id=7, credit='Artist')])
    albums.observe('B', artist='Same name', credits=[{'name': 'Same name', 'id': 'other'}], rank=1)
    albums.artists.ensure_artist.assert_not_called()
    albums.repository.store_credits.assert_not_called()


@pytest.mark.parametrize('key,expected', [('rock', {'genre_key': 'rock', 'unresolved_genre': None}),
    (None, {'genre_key': None, 'unresolved_genre': 'Unknown label'})])
def test_delivered_genre_preserves_unresolved_labels_without_inventing_an_identity(albums, key, expected):
    albums.genres.resolve.return_value = key, None
    albums.observe('B', genre='Unknown label')
    prepared = albums.repository.store_metadata.call_args.args[2]
    assert {field: prepared[field] for field in expected} == expected


def test_partial_metadata_can_fill_a_missing_field_without_replacing_the_full_genre(albums):
    albums.repository.observation_inputs.return_value = dict(album={'id': 12, 'metadata_rank': 3},
        keyed_genres=True, genre='Known', credits=[])
    albums.genres.resolve.return_value = None, 'unknown_genre'
    albums.observe('B', year=2001, genre='Unknown', rank=1)
    prepared = albums.repository.store_metadata.call_args.args[2]
    assert prepared['year'] == '2001' and 'genre_key' not in prepared


@pytest.mark.parametrize('subjects,alias,rebound', [([8], True, True), ([8], False, False), ([8, 9], True, False)])
def test_link_requires_one_verified_performer_and_an_exact_credit_alias(albums, subjects, alias, rebound):
    albums.repository.linked_artist_inputs.return_value = dict(
        credits=[dict(artist_id=7, credit='Artist', provisional=True)], subjects=subjects)
    albums.artists.has_alias.return_value = alias
    albums.artists.release_subject.return_value = 7
    albums.artists.reconcile_provisional.return_value = 8
    assert albums.link(4, 'B') == 12
    if rebound:
        albums.artists.reconcile_provisional.assert_called_once_with(7, 8, source='exact_release')
        albums.repository.rebind_artist.assert_called_once_with(12, 8)
    else:
        albums.repository.rebind_artist.assert_not_called()


def test_missing_browse_id_cannot_create_a_release(albums):
    assert albums.link(4, title='Label') is None
    with pytest.raises(ValueError, match='browse ID'):
        albums.observe(None, artist='Artist')
    albums.repository.transaction.assert_not_called()
