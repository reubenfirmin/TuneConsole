from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.playback_feedback import PlaybackFeedback
from yt_playlist.core.playback_inbox import Inbox
from yt_playlist.repos.playback_feedback import PlaybackFeedbackRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def feedback():
    repository = create_autospec(PlaybackFeedbackRepo, instance=True, spec_set=True)
    repository.try_access.return_value.__enter__.return_value = True
    repository.has_receipt.return_value = False
    queue = create_autospec(Inbox, instance=True)
    queue.replaying = Mock()
    return PlaybackFeedback(repository, queue, SimpleNamespace(activity_paused=False))


def test_receipt_is_written_after_successful_projection(feedback):
    calls = []
    feedback.repository.record_receipt.side_effect = lambda *args: calls.append('receipt')
    row = {'capture_id': 'capture'}

    def project(store, actual):
        assert store is feedback.store and actual is row
        calls.append('projection')
        return True

    assert feedback.apply_observation(row, project)
    feedback.repository.record_receipt.assert_called_once_with(row, True)
    assert calls == ['projection', 'receipt']


def test_committed_receipt_skips_projection_after_acknowledgement_failure(feedback):
    feedback.repository.has_receipt.return_value = True
    project = Mock()
    assert feedback.apply_observation({'capture_id': 'capture'}, project)
    project.assert_not_called()
    feedback.repository.record_receipt.assert_not_called()


@pytest.mark.parametrize('paused', [False, True])
def test_busy_or_paused_replay_defers_without_projecting_or_writing(feedback, paused):
    if paused:
        feedback.store.activity_paused = True
    else:
        feedback.repository.try_access.return_value.__enter__.return_value = False
    project = Mock()
    assert not feedback.apply_observation({'capture_id': 'capture'}, project)
    project.assert_not_called()
    feedback.repository.has_receipt.assert_not_called()
    feedback.repository.record_receipt.assert_not_called()


def test_projection_failure_cannot_record_a_success_receipt(feedback):
    with pytest.raises(RuntimeError, match='interrupted'):
        feedback.apply_observation({'capture_id': 'capture'}, Mock(side_effect=RuntimeError('interrupted')))
    feedback.repository.record_receipt.assert_not_called()


def test_replay_mutex_releases_after_failure(feedback):
    feedback.queue.replaying.acquire.return_value = True
    with pytest.raises(RuntimeError):
        with feedback.replaying() as admitted:
            assert admitted
            raise RuntimeError('interrupted')
    feedback.queue.replaying.acquire.assert_called_once_with(blocking=False)
    feedback.queue.replaying.release.assert_called_once()
