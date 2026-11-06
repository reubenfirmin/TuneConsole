from contextlib import contextmanager
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.charts import Charts
from yt_playlist.library.playlist_sessions import PLAYLIST_SESSIONS
from yt_playlist.repos.charts import ChartsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def charts():
    repository = create_autospec(ChartsRepo, instance=True, spec_set=True)
    return Charts(repository)


def test_fallback_labels_preserve_scoped_keys_and_nonempty_titles(charts):
    charts.repository.album_labels.return_value = {'@album:1': None, '@album:2': 'Release'}
    charts.repository.playlist_labels.return_value = {'@playlist:3': '', '@playlist:4': 'Mix'}
    assert charts.album_labels() == {'@album:1': 'Album', '@album:2': 'Release'}
    assert charts.playlist_labels() == {'@playlist:3': 'Playlist', '@playlist:4': 'Mix'}


def test_listening_domain_supplies_session_rules_to_repository(charts):
    charts.repository.get_playlist_playback_stats.return_value = {17: (200, 2)}
    assert charts.get_playlist_playback_stats() == {17: (200, 2)}
    charts.repository.get_playlist_playback_stats.assert_called_once_with(policy=PLAYLIST_SESSIONS)
    assert PLAYLIST_SESSIONS.gap_seconds == 1800


@pytest.mark.parametrize('selection', [{'artist': 'John Coltrane'}, {'genres': ['Free Jazz']}])
def test_examples_keep_repository_order_and_deduplicate_before_limit(charts, selection):
    rows = [
        {'video_id': 'first', 'title': 'Wise One', 'artist': 'John Coltrane'},
        *[{'video_id': f'copy-{index}', 'title': 'Wise One (2020 Remaster)', 'artist': 'John Coltrane'}
          for index in range(70)],
        {'video_id': 'other', 'title': 'Giant Steps', 'artist': 'John Coltrane'},
        {'video_id': 'live', 'title': 'Wise One (Live)', 'artist': 'John Coltrane'},
    ]
    consumed, active = [], set()

    @contextmanager
    def stream(**kwargs):
        active.add('cursor')
        try:
            def iterate():
                for row in rows:
                    consumed.append(row)
                    yield row
                pytest.fail('The controller read past its final example')
            yield iterate()
        finally:
            active.remove('cursor')

    charts.repository.story_track_rows.side_effect = stream
    result = charts.story_tracks(since=100, **selection)
    assert [song['video_id'] for song in result] == ['first', 'other', 'live']
    assert len(consumed) == 73
    assert not active
    charts.repository.story_track_rows.assert_called_once_with(
        since=100, artist=selection.get('artist'), genres=selection.get('genres', ()))


@pytest.mark.parametrize('kwargs', [{'limit': 0, 'artist': 'Band'}, {'limit': -1, 'genres': ['Jazz']}, {}])
def test_empty_story_request_does_not_open_a_read(charts, kwargs):
    assert charts.story_tracks(since=0, **kwargs) == []
    charts.repository.story_track_rows.assert_not_called()


def test_story_failure_releases_the_cursor(charts):
    released = []

    @contextmanager
    def stream(**kwargs):
        try:
            def iterate():
                yield {'title': 'Song', 'artist': 'Band'}
                raise RuntimeError('query interrupted')
            yield iterate()
        finally:
            released.append(True)

    charts.repository.story_track_rows.side_effect = stream
    with pytest.raises(RuntimeError, match='query interrupted'):
        charts.story_tracks(since=0, artist='Band')
    assert released == [True]
