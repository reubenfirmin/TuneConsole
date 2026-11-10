from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.search import Search
from yt_playlist.repos.search import SearchRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def search():
    repository = create_autospec(SearchRepo, instance=True, spec_set=True)
    for method in ('artist_matches', 'artist_tracks', 'artist_albums', 'artist_playlists',
                   'song_matches', 'playlist_matches'):
        getattr(repository, method).return_value = []
    return Search(repository)


def track(key, *, video=None):
    return {'key': key, 'title': key, 'artist': 'Ritmo', 'vid': video or key, 'thumb': None}


def section(result, kind):
    return next((part['rows'] for part in result['sections'] if part['kind'] == kind), [])


@pytest.mark.parametrize('query', [None, '', ' ', ' r '])
def test_short_query_does_not_query_repository(search, query):
    assert search.omni_search(query) == {'query': (query or '').strip(),
                                       'primary_artist': None, 'sections': []}
    assert search.repository.mock_calls == []


def test_artist_pivot_preserves_identity_and_constructs_associated_links(search):
    repository = search.repository
    repository.artist_matches.return_value = [
        {'artist_id': 2, 'artist': 'Ritmo', 'n': 3, 'thumb': None},
        {'artist_id': 7, 'artist': 'Ritmo', 'n': 1, 'thumb': 'art'},
    ]
    repository.artist_tracks.return_value = [track('song', video='a/b?c')]
    repository.artist_albums.return_value = [{'album': 'LP', 'browse': 'a&b', 'thumb': None}]
    repository.artist_playlists.return_value = [{'pid': 9, 'title': 'Mix', 'thumb': None}]
    result = search.omni_search('  ritmo  ')
    assert result['primary_artist'] == {'artist_id': 2, 'name': 'Ritmo', 'thumbnail': None}
    assert [(row['artist_id'], row['href'], row['sub']) for row in section(result, 'artist')] == [
        (2, '/artist?artist_id=2', '3 tracks'), (7, '/artist?artist_id=7', '1 track')]
    assert section(result, 'tracks_by')[0]['href'] == '/track/a%2Fb%3Fc'
    assert section(result, 'albums_by')[0]['href'] == '/album?browse=a%26b'
    assert section(result, 'playlists_featuring')[0]['href'] == '/playlist/9'
    assert not section(result, 'tracks_by')[0]['external']
    repository.artist_tracks.assert_called_once_with(2, limit=50)
    repository.artist_playlists.assert_called_once_with(2, excluded_group='Generated', limit=50)


def test_direct_matches_need_no_artist_pivot(search):
    search.repository.song_matches.return_value = [track('Caliente')]
    search.repository.playlist_matches.return_value = [{'pid': 2, 'title': 'Mix', 'thumb': None}]
    result = search.omni_search('caliente')
    assert result['primary_artist'] is None
    assert section(result, 'songs')[0]['label'] == 'Caliente'
    assert section(result, 'playlists')[0]['label'] == 'Mix'
    search.repository.artist_tracks.assert_not_called()


def test_deduplication_uses_identity_and_playlist_id_before_sampling(search):
    repository = search.repository
    repository.artist_matches.return_value = [{'artist_id': 2, 'artist': 'Ritmo', 'n': 3, 'thumb': None}]
    repository.artist_tracks.return_value = [track('same')]
    repository.song_matches.return_value = [track('same', video='alternate'), track('different')]
    repository.artist_playlists.return_value = [{'pid': 1, 'title': 'Mix', 'thumb': None}]
    repository.playlist_matches.return_value = [{'pid': 1, 'title': 'Mix', 'thumb': None},
                                               {'pid': 2, 'title': 'Mix', 'thumb': None}]
    result = search.omni_search('ritmo')
    assert [row['label'] for row in section(result, 'songs')] == ['different']
    assert [row['href'] for row in section(result, 'playlists')] == ['/playlist/2']


def test_sampling_is_stable_and_obeys_requested_section_size(search):
    search.repository.song_matches.return_value = [track(str(i)) for i in range(50)]
    first = search.omni_search('ritmo', per_section=5)
    assert first == search.omni_search('ritmo', per_section=5)
    assert first == search.omni_search('RITMO', per_section=5) | {'query': 'ritmo'}
    assert len(section(first, 'songs')) == 5


def test_failed_query_does_not_return_partial_sections(search):
    search.repository.song_matches.side_effect = RuntimeError('read cancelled')
    with pytest.raises(RuntimeError, match='read cancelled'):
        search.omni_search('ritmo')
    search.repository.playlist_matches.assert_not_called()
