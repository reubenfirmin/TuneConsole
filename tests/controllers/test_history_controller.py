"""Listening source interpretation over explicit mocked persistence inputs."""
from contextlib import nullcontext
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.artists import Artists
from yt_playlist.controllers.history import History
from yt_playlist.controllers.history_maintenance import HistoryMaintenance
from yt_playlist.repos.history import HistoryRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def history():
    repo = create_autospec(HistoryRepo, instance=True, spec_set=True)
    repo.calendar_facts.return_value = {'timezone': 'UTC', 'confirmed': 1}
    repo.playlist_quarantined.return_value = False
    return History(repo, create_autospec(Artists, instance=True, spec_set=True),
        create_autospec(HistoryMaintenance, instance=True, spec_set=True), lambda: {'radio'}, nullcontext)


def test_dated_captures_keep_source_precision_and_drop_unknown_labels(history):
    history.record_history_plays(3, 10 * 86400, [
        'independent', ('dated', 'Yesterday'), ('missing', None), ('unknown', 'Earlier'),
        ('weekly', 'This week'), ('', 'Today')], source_timezone='UTC')
    history.repository.record_days.assert_called_once_with(3, 10 * 86400,
        [('independent', '1970-01-11', 'independent'), ('dated', '1970-01-10', 'ytm_history')], 'UTC')
    history.repository.import_events.assert_not_called()


@pytest.mark.parametrize('current,source,name,confirmed,reproject', [
    (None, 'America/Costa_Rica', 'America/Costa_Rica', True, False),
    (None, None, 'UTC', False, False),
    ({'timezone': 'UTC', 'confirmed': 0}, 'America/Costa_Rica', 'America/Costa_Rica', True, True),
])
def test_first_explicit_calendar_reprojects_only_existing_unconfirmed_state(
        history, monkeypatch, current, source, name, confirmed, reproject):
    monkeypatch.setattr('yt_playlist.controllers.history.machine_timezone', lambda: 'UTC')
    history.repository.calendar_facts.return_value = current
    history.import_listening_events(3, [], source_timezone=source)
    history.repository.set_calendar.assert_called_once_with(3, name, confirmed=confirmed, reproject=reproject)


def test_later_source_calendar_dates_are_retained_without_replacing_account_calendar(history):
    history.record_history_plays(3, 86400, [('song', 'Today')], source_timezone='America/Costa_Rica')
    history.repository.set_calendar.assert_not_called()
    history.repository.record_days.assert_called_once_with(3, 86400,
        [('song', '1970-01-01', 'ytm_history')], 'America/Costa_Rica')


@pytest.mark.parametrize('playlist,quarantined,context,expected', [
    (None, False, False, 0), ('ordinary', False, False, 0),
    ('radio', False, False, 1), ('RDAMPLradio', False, False, 1),
    ('generated', True, False, 1), ('ordinary', False, True, 1),
])
def test_live_provenance_and_replay_windows_are_controller_policy(history, playlist, quarantined, context, expected):
    history.repository.playlist_quarantined.return_value = quarantined
    history.record_play_event(3, 'song', 'video', 100, playlist, 'LIKE', context_choice=context)
    history.repository.record_live.assert_called_once_with(3, 'song', 'video', 100, playlist, 'LIKE',
        is_generated=expected, exact_tolerance=2.0, live_window=1800)


def test_spotify_artist_admission_finishes_before_receipt_write(history):
    history.artists.state_id.return_value = 8
    order = Mock()
    order.attach_mock(history.artists.state_id, 'artist')
    order.attach_mock(history.repository.import_spotify_receipts, 'receipts')
    history.import_spotify_listens(3, [('uri', 1, 'Unknown', None), ('uri', 2, 'Unknown', None),
                                      ('other', 3, 'Known', 'song')])
    assert [call[0] for call in order.mock_calls] == ['artist', 'receipts']
    history.repository.import_spotify_receipts.assert_called_once_with(3,
        [('uri', 1, 8, None), ('uri', 2, 8, None), ('other', 3, None, 'song')],
        source='spotify_export', exact_tolerance=2.0)


def test_failed_artist_admission_does_not_publish_receipts(history):
    history.artists.state_id.side_effect = ValueError('unknown artist reference')
    with pytest.raises(ValueError):
        history.import_spotify_listens(3, [('uri', 1, '@artist:404', None)])
    history.repository.import_spotify_receipts.assert_not_called()


@pytest.mark.parametrize('source', ['', 'unknown', 'independent'])
def test_unknown_provenance_is_never_assigned_to_a_removable_import(history, source):
    with pytest.raises(ValueError, match='provenance'):
        history.remove_listening_source(source)
    history.repository.remove_listening_source.assert_not_called()


def test_compatibility_imports_return_the_requested_measurement(history):
    history.repository.import_events.return_value = (2, 5)
    assert history.import_plays(3, [('song', 100)]) == 2
    assert list(history.repository.import_events.call_args.args[1]) == [('song', None, 100)]
    assert history.import_play_events(3, [('song', 'video', 100)]) == 5
