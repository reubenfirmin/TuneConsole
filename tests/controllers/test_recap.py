from datetime import datetime, timezone
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.genres import Genres
from yt_playlist.controllers.recap import Recap
from yt_playlist.rec import recap_evidence
from yt_playlist.repos.recap import RecapRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def recap():
    repository = create_autospec(RecapRepo, instance=True, spec_set=True)
    for name in ('plays', 'track_metadata', 'track_albums', 'track_artists', 'full_album_tracks', 'created_playlists'):
        getattr(repository, name).return_value = []
    for name in ('play_sources', 'first_plays', 'first_observations', 'source_spans', 'artist_labels', 'calendars'):
        getattr(repository, name).return_value = {}
    repository.familiar_artists.return_value = set()
    genres = create_autospec(Genres, instance=True, spec_set=True)
    genres.feature_axes.return_value = {'Jazz': ('jazz', 'jazz')}
    return Recap(repository, genres)


def test_recap_combines_observations_without_inventing_plays(recap):
    recap.repository.plays.return_value = [{'id': 8, 'identity_key': 'song', 'played_at': 150}]
    recap.repository.play_sources.return_value = {8: ['takeout', 'live']}
    recap.repository.first_plays.return_value = {'song': 100, 'new': 200}
    recap.repository.first_observations.return_value = {'song': 50, 'old': 10}
    recap.repository.source_spans.return_value = {(1, 'generated-source'): (0, 200)}
    result = recap.snapshot(100, 200)
    assert result['plays'] == [{'identity_key': 'song', 'played_at': 150, 'sources': ('live', 'takeout')}]
    assert result['first'] == {'song': 50, 'new': 200, 'old': 10}
    assert result['spans'] == {(1, 'generated-source'): (0, 200)}
    recap.repository.plays.assert_called_once_with(
        100, 200, taste_filter=recap_evidence.TASTE_FILTER, quarantine_group='Generated')
    recap.repository.play_sources.assert_called_once_with([8])
    recap.repository.source_spans.assert_called_once_with()


def test_recap_selects_metadata_fields_without_inventing_unique_release_context(recap):
    recap.repository.track_metadata.return_value = [
        {'identity_key': 'song', 'title': 'First title', 'artist': '', 'genre': '', 'mb_year': ''},
        {'identity_key': 'song', 'title': 'Alternate', 'artist': 'Artist', 'genre': 'Jazz', 'mb_year': '1991'},
        {'identity_key': 'other', 'title': '', 'artist': '', 'genre': '', 'mb_year': ''}]
    recap.repository.track_albums.return_value = [('song', 'a'), ('song', 'b'), ('other', 'c')]
    recap.repository.track_artists.return_value = [('song', 4), ('song', 5), ('missing', 6)]
    result = recap.snapshot(0, 200)['meta']
    assert result['song'] == {'title': 'First title', 'artist': 'Artist', 'genre': 'Jazz', 'mb_year': '1991',
                              'album_browse_ids': {'a', 'b'}, 'album_browse_id': None,
                              'artist_ids': [4, 5], 'family': 'jazz'}
    assert result['other'] == {'album_browse_ids': {'c'}, 'album_browse_id': 'c', 'family': ''}


def test_recap_groups_complete_album_tracks_and_preserves_familiar_artist_facts(recap):
    album = {'id': 7, 'title': 'Album', 'artist': 'Artist', 'thumbnail': None, 'browse_id': 'release'}
    recap.repository.full_album_tracks.return_value = [{**album, 'track_key': key} for key in ('a', 'a', 'b')]
    recap.repository.familiar_artists.return_value = {4, 5}
    result = recap.snapshot(0, 200)
    assert result['albums'] == [{'title': 'Album', 'artist': 'Artist', 'thumbnail': None,
                                 'browse_id': 'release', 'keys': {'a', 'b'}}]
    assert result['familiar_artists'] == {4, 5}
    recap.repository.familiar_artists.assert_called_once_with(quarantine_group='Generated')


def test_playlist_calendar_conversion_and_query_margin_belong_to_recap(recap):
    created = datetime(2026, 4, 1, 0, 30, tzinfo=timezone.utc).timestamp()
    recap.repository.created_playlists.return_value = [{'account_id': 2, 'created_at': created},
                                                       {'account_id': 3, 'created_at': created}]
    recap.repository.calendars.return_value = {2: 'America/Costa_Rica'}
    result = recap.snapshot(100000, 200000)['playlists']
    assert result == [{'created_at': created, 'day': '2026-03-31'}, {'created_at': created, 'day': '2026-04-01'}]
    recap.repository.created_playlists.assert_called_once_with(
        13600, 286400, quarantine_group='Generated', creation_sources=('local', 'youtube'))
