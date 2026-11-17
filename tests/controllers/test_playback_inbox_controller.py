from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.playback_inbox import PlaybackInbox
from yt_playlist.core.playback_observation_files import encode, relative

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def inbox(monkeypatch):
    monkeypatch.setattr('yt_playlist.controllers.playback_inbox.uuid.uuid4', lambda: SimpleNamespace(hex='a' * 32))
    repository = SimpleNamespace(path=Path('/inbox.db'), reserve=Mock(return_value=True),
        complete=Mock(), recovery_page=Mock(return_value={'after': 0, 'pending': []}),
        next=Mock(), acknowledge=Mock())
    reference = ('/observations/aa/' + 'a' * 32 + '.json', 'digest', 123)
    files = SimpleNamespace(root=Path('/observations'), lock=Mock(), admission=lambda: nullcontext(),
        retained=lambda: nullcontext(),
        recovery=Mock(side_effect=lambda: nullcontext(True)), reference=Mock(return_value=reference),
        publish=Mock(return_value=reference), delete=Mock(), body=Mock())
    return PlaybackInbox(repository, files)


def test_capture_has_a_committed_database_owner_before_file_access_and_becomes_ready_last(inbox):
    events = []
    inbox.repository.reserve.side_effect = lambda *args: events.append('record') or True
    inbox.files.publish.side_effect = lambda *args: events.append('file') or inbox.files.reference.return_value
    inbox.repository.complete.side_effect = lambda *args: events.append('ready')
    capture = inbox.capture('player', 7, {'videoId': 'song'}, 100)
    assert capture == 'a' * 32 and events == ['record', 'file', 'ready']
    assert inbox.repository.reserve.call_args.args[:6] == (capture, 'player', 7, 100, 'song', None)
    inbox.files.publish.assert_called_once_with(capture, 'player', {'videoId': 'song'})
    inbox.repository.complete.assert_called_once_with(capture)


@pytest.mark.parametrize('failure', ['reserve', 'file'])
def test_failed_capture_never_claims_that_its_file_is_ready(inbox, failure):
    target = inbox.repository.reserve if failure == 'reserve' else inbox.files.publish
    target.side_effect = OSError('interrupted')
    with pytest.raises(OSError, match='interrupted'):
        inbox.capture('player', 7, {}, 100)
    inbox.repository.complete.assert_not_called()
    if failure == 'reserve':
        inbox.files.publish.assert_not_called()


def test_installed_inline_capture_continues_writing_without_file_conversion(inbox):
    inbox.repository.reserve.return_value = False
    inbox.capture('player', 7, {}, 100)
    inbox.files.publish.assert_not_called()
    inbox.repository.complete.assert_not_called()


def test_restart_reconciles_only_complete_verified_observations_before_replay(inbox):
    missing, corrupt, complete = ({'capture_id': identity, 'sequence': index}
        for index, identity in enumerate(('missing', 'corrupt', 'complete'), 1))
    inbox.repository.recovery_page.return_value = {'after': 0, 'pending': [missing, corrupt, complete]}
    inbox.files.body.side_effect = [FileNotFoundError(), ValueError('checksum'), {'videoId': 'song'}]
    assert inbox.next() is inbox.repository.next.return_value
    inbox.repository.complete.assert_called_once_with('complete')
    inbox.files.publish.assert_not_called()


def test_recovery_defers_an_active_capture_instead_of_marking_it_ready(inbox):
    inbox.files.recovery.side_effect = lambda: nullcontext(False)
    inbox.next()
    inbox.repository.recovery_page.assert_not_called()
    inbox.repository.complete.assert_not_called()


def test_missing_files_do_not_starve_later_recovery_pages(inbox):
    inbox.repository.recovery_page.side_effect = [
        {'after': 32, 'pending': [dict(sequence=index, capture_id=str(index)) for index in range(1, 33)]},
        {'after': 0, 'pending': [dict(sequence=33, capture_id='complete')]},
    ]
    inbox.files.body.side_effect = [*[FileNotFoundError()] * 32, {}]
    inbox.next()
    inbox.next()
    assert [call.kwargs for call in inbox.repository.recovery_page.call_args_list] == [{'after': 0}, {'after': 32}]
    inbox.repository.complete.assert_called_once_with('complete')


def test_acknowledgement_commits_before_file_expiry(inbox):
    events = []
    inbox.repository.acknowledge.side_effect = lambda capture: events.append('receipt') or '/observation'
    inbox.files.delete.side_effect = lambda filename: events.append('delete')
    inbox.acknowledge('capture')
    assert events == ['receipt', 'delete']


@pytest.mark.parametrize('capture', ['../outside', '', 'a' * 64, 1])
def test_paths_require_the_capture_id_instead_of_a_digest_or_supplied_path(capture):
    with pytest.raises(ValueError, match='capture ID'):
        relative(capture)


def test_path_identity_is_independent_of_content():
    capture = 'b' * 32
    assert relative(capture) == Path('bb', capture + '.json')
    assert encode(capture, 'player', {'videoId': 'first'}) != encode(capture, 'player', {'videoId': 'second'})
