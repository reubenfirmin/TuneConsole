"""Calendar anchoring rules use detached evidence without a database."""
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.listening_calendar import ListeningCalendar
from yt_playlist.repos.listening_calendar import ListeningCalendarRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def calendar():
    return ListeningCalendar(create_autospec(ListeningCalendarRepo, instance=True, spec_set=True))


@pytest.mark.parametrize('days,expected', [([], None), (['1970-01-01'], '1970-01-01'),
    (['1970-01-01', '1970-01-02'], None), (['1970-01-01', '1970-01-01'], '1970-01-01')])
def test_only_one_distinct_exact_calendar_day_anchors_a_foreign_date(calendar, days, expected):
    calendar.repository.foreign_observations.return_value = [
        {'id': 1, 'identity_key': 'song', 'source_day': '1970-01-01', 'timezone': 'UTC', 'day': 'old'},
        {'id': 2, 'identity_key': 'song', 'source_day': '1970-01-01', 'timezone': 'UTC', 'day': 'old'}]
    calendar.repository.exact_days.return_value = [{'played_at': 100, 'day': day} for day in days]
    calendar.reconcile(7, observations={('song', '1970-01-01', 'UTC')})
    calendar.repository.exact_days.assert_called_once_with(7, 'song', 0, 86400)
    calendar.repository.store_days.assert_called_once_with([(expected, 1), (expected, 2)])


def test_event_scopes_skip_unrelated_source_dates_and_keep_existing_projection(calendar):
    calendar.repository.foreign_observations.return_value = [
        {'id': 1, 'identity_key': 'song', 'source_day': '1970-01-01', 'timezone': 'UTC', 'day': '1970-01-01'},
        {'id': 2, 'identity_key': 'song', 'source_day': '1970-01-02', 'timezone': 'UTC', 'day': None}]
    calendar.repository.exact_days.return_value = [{'played_at': 100, 'day': '1970-01-01'},
        {'played_at': 86400, 'day': '1970-01-02'}]
    calendar.reconcile(7, events={('song', 100)})
    calendar.repository.foreign_observations.assert_called_once_with(7, observations=None, keys={'song'})
    calendar.repository.exact_days.assert_called_once_with(7, 'song', 0, 86400)
    calendar.repository.store_days.assert_not_called()


def test_recording_reconciliation_groups_scopes_by_account(calendar):
    calendar.repository.recording_scopes.return_value = [
        {'account_id': 7, 'identity_key': 'song', 'source_day': '1970-01-01', 'timezone': 'UTC'},
        {'account_id': 7, 'identity_key': 'song', 'source_day': '1970-01-01', 'timezone': 'UTC'}]
    calendar.repository.foreign_observations.return_value = []
    calendar.reconcile_recordings({2, 3})
    calendar.repository.recording_scopes.assert_called_once_with([2, 3])
    calendar.repository.foreign_observations.assert_called_once_with(7,
        observations={('song', '1970-01-01', 'UTC')}, keys=None)
