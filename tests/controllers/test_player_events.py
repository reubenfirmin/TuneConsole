from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.player_events import PlayerEvents
from yt_playlist.library.listen_derive import SKIP_HISTORY_DAYS
from yt_playlist.repos.player_events import PlayerEventsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def events():
    return PlayerEvents(create_autospec(PlayerEventsRepo, instance=True, spec_set=True))


def test_only_qualifying_exits_count_and_latest_qualifying_exit_wins(events):
    events.repository.library_exits.return_value = [
        {'key': 'a', 'position': 190, 'duration': 200, 'at': 50},
        {'key': 'b', 'position': 20, 'duration': 200, 'at': 40},
        {'key': 'a', 'position': 10, 'duration': 200, 'at': 30},
        {'key': 'a', 'position': 10, 'duration': 200, 'at': 20},
        {'key': None, 'position': 10, 'duration': 200, 'at': 10},
    ]
    assert events.recent_skips_with_ts(5) == [('b', 40.), ('a', 30.)]
    events.repository.library_exits.assert_called_once_with(5)


def test_empty_key_selection_does_not_read_history(events):
    assert events.skip_blackouts(100, keys=[]) == {}
    events.repository.recording_exits.assert_not_called()


def test_blackouts_request_complete_bounded_history_and_collapse_same_playback(events):
    events.repository.recording_exits.return_value = [
        {'key': 'a', 'account_id': 1, 'kind': 'next', 'position': 20, 'duration': 200, 'at': 100},
        {'key': 'a', 'account_id': 1, 'kind': 'bye', 'position': 21, 'duration': 200, 'at': 101},
    ]
    assert events.skip_blackouts(102, keys=['a', 'a']) == {'a': 100 + 86400}
    events.repository.recording_exits.assert_called_once_with(
        102 - SKIP_HISTORY_DAYS * 86400, 102, keys=['a'])
