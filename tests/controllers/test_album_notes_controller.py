"""Editorial policy and file ordering with mocked storage owners."""
from contextlib import contextmanager
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.album_notes import AlbumNotes
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.repos.album_notes import AlbumNotesRepo

pytestmark = pytest.mark.usefixtures('no_database')
ALBUM = {'browse_id': 'release', 'artist': 'Artist', 'title': 'Release', 'year': '2020', 'thumbnail': None}


@pytest.fixture
def notes():
    repo = create_autospec(AlbumNotesRepo, instance=True, spec_set=True)
    repo.requests.return_value = {}
    repo.matches.return_value = True
    writing = False

    @contextmanager
    def transaction():
        nonlocal writing
        writing = True
        try:
            yield
        finally:
            writing = False

    def publish(encoded):
        assert not writing, 'Editorial files must be published before opening the SQL unit'
        return ('file', 'digest', 10)

    repo.transaction.side_effect = transaction
    files = create_autospec(RecommendationFiles, instance=True, spec_set=True)
    files.publish.side_effect = publish
    proposals = Mock()
    proposals.get_proposals.return_value = None
    return AlbumNotes(repo, proposals, files, installed=True, paused=lambda: False)


@pytest.mark.parametrize('cached,admit', [
    ({'editorial_version': 7, 'retry_at': 101}, False),
    ({'editorial_version': 7, 'retry_at': 100}, True),
    ({'editorial_version': 6, 'retry_at': 101}, True),
])
def test_editorial_version_and_retry_control_admission(notes, cached, admit):
    notes.proposals.get_proposals.return_value = {'release': cached}
    notes.request([ALBUM], 100)
    assert bool(notes.repository.store_request.call_count) == admit


def test_visible_poll_extends_visibility_without_changing_worker_input(notes):
    notes.repository.requests.return_value = {'release': {**ALBUM, 'requested_until': 1000}}
    notes.request([ALBUM], 200, visible=True)
    fields = notes.repository.store_request.call_args.args[0]
    assert fields[-1] == 1100
    assert notes.repository.store_request.call_args.kwargs == {'changed_work': False}


def test_metadata_change_requires_a_new_worker_revision(notes):
    notes.repository.requests.return_value = {'release': ALBUM}
    notes.request([{**ALBUM, 'year': '2021'}], 100)
    assert notes.repository.store_request.call_args.kwargs == {'changed_work': True}


def test_current_menu_keeps_visible_demand_and_removes_expired_demand(notes):
    notes.repository.requests.return_value = {
        'visible': {**ALBUM, 'browse_id': 'visible', 'requested_until': 101},
        'expired': {**ALBUM, 'browse_id': 'expired', 'requested_until': 100}}
    notes.request([], 100, current_only=True)
    notes.repository.remove_request.assert_called_once_with('expired')


def test_visible_demand_is_coalesced_bounded_and_expires(notes):
    notes.installed = False
    values = {}
    notes.proposals.get_proposals.side_effect = values.get
    notes.proposals.put_proposals.side_effect = lambda surface, value, now: values.update({surface: value})
    albums = [{**ALBUM, 'browse_id': f'visible-{index}'} for index in range(30)]
    notes.request(albums, 1, visible=True)
    notes.request([albums[-1]], 10, visible=True)
    queued = values['album_note_requests']
    assert len(queued) == 24 and queued['visible-29']['requested_until'] == 910
    notes.request([], 902, current_only=True)
    assert set(values['album_note_requests']) == {'visible-29'}
    notes.request([], 911, current_only=True)
    assert not values['album_note_requests']


def test_publication_saves_file_before_sql_reference_and_exact_request_completion(notes):
    assert notes.publish('release', {'label': 'Current'}, 100, expected_request=(7, 2))
    notes.repository.replace_output.assert_called_once_with('release', ('file', 'digest', 10), 100, 128)
    notes.repository.remove_request.assert_called_once_with('release', (7, 2))


@pytest.mark.parametrize('matches,file_written', [([False], False), ([True, False], True)])
def test_superseded_worker_cannot_publish_or_complete_newer_demand(notes, matches, file_written):
    notes.repository.matches.side_effect = matches
    assert not notes.publish('release', {'label': 'Stale'}, 100, expected_request=(7, 2))
    assert bool(notes.files.publish.call_count) == file_written
    notes.repository.replace_output.assert_not_called()
    notes.repository.remove_request.assert_not_called()


def test_file_failure_preserves_last_good_and_demand(notes):
    notes.files.publish.side_effect = OSError('disk unavailable')
    with pytest.raises(OSError, match='disk unavailable'):
        notes.publish('release', {'label': 'New'}, 100, expected_request=(7, 2))
    notes.repository.transaction.assert_not_called()
    notes.repository.replace_output.assert_not_called()
    notes.repository.remove_request.assert_not_called()


def test_paused_work_leaves_storage_untouched(notes):
    notes.paused = lambda: True
    notes.request([], 100, current_only=True)
    assert not notes.publish('release', {}, 100)
    notes.proposals.get_proposals.assert_not_called()
    notes.repository.transaction.assert_not_called()
    notes.files.publish.assert_not_called()
