"""Editorial policy and publication with mocked storage owners."""
from contextlib import nullcontext
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.album_notes import AlbumNotes
from yt_playlist.repos.album_notes import AlbumNotesRepo

pytestmark = pytest.mark.usefixtures('no_database')
ALBUM = {'browse_id': 'release', 'artist': 'Artist', 'title': 'Release', 'year': '2020', 'thumbnail': None}


@pytest.fixture
def notes():
    repo = create_autospec(AlbumNotesRepo, instance=True, spec_set=True)
    repo.requests.return_value = {}
    repo.publish_output.return_value = True
    repo.transaction.side_effect = nullcontext
    proposals = Mock()
    proposals.get_proposals.return_value = None
    return AlbumNotes(repo, proposals, paused=lambda: False)


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


def test_metadata_change_reaches_the_provider_owner(notes):
    notes.repository.requests.return_value = {'release': ALBUM}
    notes.request([{**ALBUM, 'year': '2021'}], 100)
    assert notes.repository.store_request.call_args.args[0][3] == '2021'


def test_current_menu_keeps_visible_demand_and_removes_expired_demand(notes):
    notes.repository.requests.return_value = {
        'visible': {**ALBUM, 'browse_id': 'visible', 'requested_until': 101},
        'expired': {**ALBUM, 'browse_id': 'expired', 'requested_until': 100}}
    notes.request([], 100, current_only=True)
    notes.repository.remove_request.assert_called_once_with('expired')


def test_visible_demand_is_coalesced_bounded_and_expires(notes):
    queued = {}
    notes.repository.requests.side_effect = lambda: dict(queued)
    notes.repository.remove_request.side_effect = queued.pop

    def remember(fields):
        browse, artist, title, year, _, thumbnail, until = fields
        queued[browse] = {'browse_id': browse, 'artist': artist, 'title': title, 'year': year,
                          'thumbnail': thumbnail, 'requested_until': until}

    notes.repository.store_request.side_effect = remember
    albums = [{**ALBUM, 'browse_id': f'visible-{index}'} for index in range(30)]
    notes.request(albums, 1, visible=True)
    notes.request([albums[-1]], 10, visible=True)
    assert len(queued) == 24 and queued['visible-29']['requested_until'] == 910
    notes.request([], 902, current_only=True)
    assert set(queued) == {'visible-29'}
    notes.request([], 911, current_only=True)
    assert not queued


def test_publication_passes_the_output_and_exact_worker_token(notes):
    assert notes.publish('release', {'label': 'Current'}, 100, expected_request=(7, 2))
    browse, raw = notes.repository.publish_output.call_args.args
    assert browse == 'release' and json.loads(raw) == {'label': 'Current'}
    assert notes.repository.publish_output.call_args.kwargs == {'expected_request': (7, 2)}


def test_superseded_worker_cannot_publish_or_complete_newer_demand(notes):
    notes.repository.publish_output.return_value = False
    assert not notes.publish('release', {'label': 'Stale'}, 100, expected_request=(7, 2))
    notes.repository.replace_outputs.assert_not_called()
    notes.repository.remove_request.assert_not_called()


def test_publication_failure_leaves_demand_to_the_atomic_writer(notes):
    notes.repository.publish_output.side_effect = RuntimeError('write failed')
    with pytest.raises(RuntimeError, match='write failed'):
        notes.publish('release', {'label': 'New'}, 100, expected_request=(7, 2))
    notes.repository.transaction.assert_not_called()
    notes.repository.replace_outputs.assert_not_called()
    notes.repository.remove_request.assert_not_called()


def test_paused_work_leaves_storage_untouched(notes):
    notes.paused = lambda: True
    notes.request([], 100, current_only=True)
    assert not notes.publish('release', {}, 100)
    notes.proposals.get_proposals.assert_not_called()
    notes.repository.transaction.assert_not_called()
    notes.repository.publish_output.assert_not_called()
