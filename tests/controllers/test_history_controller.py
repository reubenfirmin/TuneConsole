"""Listening source interpretation over explicit mocked persistence inputs."""
from contextlib import nullcontext
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.artists import Artists
from yt_playlist.controllers.history import History
from yt_playlist.controllers.history_maintenance import HistoryMaintenance
from yt_playlist.controllers.listening_calendar import ListeningCalendar
from yt_playlist.repos.history import HistoryRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def history():
    repo = create_autospec(HistoryRepo, instance=True, spec_set=True)
    repo.calendar_facts.return_value = {'timezone': 'UTC', 'confirmed': 1}
    repo.playlist_quarantined.return_value = False
    repo.transaction.side_effect = nullcontext
    repo.calendar_events.return_value = []
    repo.day_count.return_value = 0
    repo.matching_event.return_value = repo.latest_event.return_value = repo.spotify_receipt.return_value = None
    repo.subject_facts.return_value = {'direct': None, 'exact': None, 'aliases': []}
    return History(repo, create_autospec(Artists, instance=True, spec_set=True),
        create_autospec(HistoryMaintenance, instance=True, spec_set=True), lambda: {'radio'}, nullcontext,
        create_autospec(ListeningCalendar, instance=True, spec_set=True))


def test_dated_captures_keep_source_precision_and_drop_unknown_labels(history):
    history.record_history_plays(3, 10 * 86400, [
        'independent', ('dated', 'Yesterday'), ('missing', None), ('unknown', 'Earlier'),
        ('weekly', 'This week'), ('', 'Today')], source_timezone='UTC')
    history.repository.record_days.assert_called_once_with(3, 10 * 86400,
        [('independent', 'independent', None, '1970-01-11', '1970-01-11', 'independent'),
         ('dated', 'dated', None, '1970-01-10', '1970-01-10', 'ytm_history')], 'UTC')
    history.repository.append_event.assert_not_called()


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
    history.repository.set_calendar.assert_called_once_with(3, name, confirmed=confirmed, event_days=[] if reproject else None)


def test_later_source_calendar_dates_are_retained_without_replacing_account_calendar(history):
    history.record_history_plays(3, 86400, [('song', 'Today')], source_timezone='America/Costa_Rica')
    history.repository.set_calendar.assert_not_called()
    history.repository.record_days.assert_called_once_with(3, 86400,
        [('song', 'song', None, '1970-01-01', None, 'ytm_history')], 'America/Costa_Rica')
    history.calendar.reconcile.assert_called_once_with(3,
        observations={('song', '1970-01-01', 'America/Costa_Rica')})


@pytest.mark.parametrize('playlist,quarantined,context,expected', [
    (None, False, False, 0), ('ordinary', False, False, 0),
    ('radio', False, False, 1), ('RDAMPLradio', False, False, 1),
    ('generated', True, False, 1), ('ordinary', False, True, 1),
])
def test_live_provenance_and_replay_windows_are_controller_policy(history, playlist, quarantined, context, expected):
    history.repository.playlist_quarantined.return_value = quarantined
    history.record_play_event(3, 'song', 'video', 100, playlist, 'LIKE', context_choice=context)
    history.repository.append_event.assert_called_once_with(3, 'song', 100, video_id='video',
        playlist_ytm_id=playlist, like_status='LIKE', is_generated=expected, subject='song', recording=None)
    history.repository.matching_event.assert_called_once_with(3, 'song', 'video', 100, 2.0)


def test_spotify_artist_admission_finishes_before_receipt_write(history):
    history.artists.state_id.return_value = 8
    order = Mock()
    order.attach_mock(history.artists.state_id, 'artist')
    order.attach_mock(history.repository.store_spotify_receipt, 'receipt')
    history.import_spotify_listens(3, [('uri', 1, 'Unknown', None), ('uri', 2, 'Unknown', None),
                                      ('other', 3, 'Known', 'song')])
    assert [call[0] for call in order.mock_calls] == ['artist', 'receipt', 'receipt', 'receipt']
    assert [call.args for call in history.repository.store_spotify_receipt.call_args_list] == [
        (3, 'uri', 1, None, 8), (3, 'uri', 2, None, 8),
        (3, 'other', 3, history.repository.append_event.return_value, None)]


def test_failed_artist_admission_does_not_publish_receipts(history):
    history.artists.state_id.side_effect = ValueError('unknown artist reference')
    with pytest.raises(ValueError):
        history.import_spotify_listens(3, [('uri', 1, '@artist:404', None)])
    history.repository.store_spotify_receipt.assert_not_called()


@pytest.mark.parametrize('source', ['', 'unknown', 'independent'])
def test_unknown_provenance_is_never_assigned_to_a_removable_import(history, source):
    with pytest.raises(ValueError, match='provenance'):
        history.remove_listening_source(source)
    history.repository.remove_listening_source.assert_not_called()


def test_compatibility_imports_return_the_requested_measurement(history):
    history.repository.day_count.side_effect = [1, 3, 3, 4]
    assert history.import_plays(3, [('song', 100)]) == 2
    history.repository.append_event.assert_called_once_with(3, 'song', 100, video_id=None, subject='song', recording=None)
    assert history.import_play_events(3, [('song', 'video', 100)]) == 1


@pytest.mark.parametrize('video,facts,subject,recording', [
    ('video', {'direct': {'id': 7, 'identity_key': 'text'},
               'exact': {'id': 8, 'identity_key': 'exact'}, 'aliases': []}, 'exact', 8),
    ('unknown', {'direct': {'id': 7, 'identity_key': 'text'}, 'exact': None, 'aliases': []},
     '@unresolved-video:unknown', None),
    (None, {'direct': None, 'exact': None, 'aliases': [{'id': 8, 'identity_key': 'only'}]}, 'only', 8),
    (None, {'direct': None, 'exact': None,
            'aliases': [{'id': 8, 'identity_key': 'a'}, {'id': 9, 'identity_key': 'b'}]}, 'raw', None),
])
def test_subject_resolution_preserves_exact_uploads_and_ambiguous_text(history, video, facts, subject, recording):
    history.repository.subject_facts.return_value = facts
    history.record_play_event(3, 'raw', video, 100)
    assert history.repository.append_event.call_args.kwargs['subject'] == subject
    assert history.repository.append_event.call_args.kwargs['recording'] == recording
    history.calendar.reconcile.assert_called_once_with(3, events={(subject, 100)})


@pytest.mark.parametrize('elapsed,new', [(1799, False), (1800, True), (-1, True)])
def test_live_deduplication_expires_at_the_session_boundary(history, elapsed, new):
    history.repository.latest_event.return_value = {
        'id': 8, 'identity_key': 'song', 'video_id': 'video', 'played_at': 100, 'live': True}
    assert history.record_play_event(3, 'song', 'video', 100 + elapsed) is new
    assert history.repository.append_event.called is new
