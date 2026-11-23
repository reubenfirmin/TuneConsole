"""TV policy checks over detached facts and mocked SQL persistence."""
from contextlib import nullcontext
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.tv_state import TVState
from yt_playlist.repos.tv_state import TVStateRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def tv():
    repo = create_autospec(TVStateRepo, instance=True, spec_set=True)
    repo.transaction.side_effect = nullcontext
    repo.receipt_header.return_value = repo.launch.return_value = None
    repo.pending_launches.return_value = 0
    return TVState(repo)


def test_invalid_visual_does_not_enter_sql(tv):
    with pytest.raises(ValueError):
        tv.publish_visual('visual00001', {'verdict': 'unsupported'}, 100)
    tv.repository.transaction.assert_not_called()
    tv.repository.replace_visual.assert_not_called()


@pytest.mark.parametrize('complete', [True, False])
def test_artwork_completion_is_fenced_by_the_observed_revision(tv, complete):
    tv.publish_artwork('artist', {'images': []}, 100, 'observed', complete=complete)
    tv.repository.replace_artwork.assert_called_once_with('artist', {'images': []}, 100, 'observed',
        name=None, complete=complete, limit=60)


@pytest.mark.parametrize('count,admitted', [(59, True), (60, False), (61, False)])
def test_pending_creation_limit_does_not_erase_unconfirmed_intents(tv, count, admitted):
    tv.repository.pending_launches.return_value = count
    selection = [{'video_id': 'first'}]
    if admitted:
        assert tv.begin_launch('refill:request', 1, 'next', 15, selection, 100, current=False)
        tv.repository.insert_launch.assert_called_once_with('refill:request', 1, 'next', 15, selection, 100, current=False)
    else:
        with pytest.raises(ValueError, match='unresolved TV'):
            tv.begin_launch('refill:request', 1, 'next', 15, selection, 100, current=False)
        tv.repository.insert_launch.assert_not_called()


@pytest.mark.parametrize('previous,message', [
    ({'id': 8, 'status': 'complete', 'target_stream': 'next'}, 'already completed'),
    ({'id': 8, 'status': 'pending', 'target_stream': 'other'}, 'another intent'),
])
def test_receipt_completion_cannot_replace_completed_or_foreign_intents(tv, previous, message):
    tv.repository.receipt_header.return_value = previous
    with pytest.raises(ValueError, match=message):
        tv.complete_receipt('10:stream:request', {'playlist_id': 20, 'stream': 'next'}, 100)
    tv.repository.complete_receipt.assert_not_called()


@pytest.mark.parametrize('items', [[], [{'video_id': 'same'}, {'video_id': 'same'}],
    [{'video_id': str(index)} for index in range(61)]])
def test_launch_requires_a_bounded_distinct_selection(tv, items):
    with pytest.raises(ValueError, match='bounded, distinct'):
        tv.begin_launch('request', 1, 'stream', 30, items, 100)
    tv.repository.insert_launch.assert_not_called()


def test_existing_launch_preserves_its_original_selection(tv):
    tv.repository.launch.return_value = {'items': [{'video_id': 'original'}]}
    assert not tv.begin_launch('request', 1, 'stream', 30, [{'video_id': 'replacement'}], 100)
    tv.repository.insert_launch.assert_not_called()


def test_completion_confirms_only_the_videos_the_workflow_proved(tv):
    tv.complete_launch('request', ['delivered'])
    assert tv.repository.method_calls[1:3] == [
        ('set_outcome', ('request', {'confirmed': ['delivered']}), {}),
        ('complete_launch', ('request',), {}),
    ]


def test_failed_outcome_write_does_not_mark_launch_complete(tv):
    tv.repository.set_outcome.side_effect = RuntimeError('interrupted')
    with pytest.raises(RuntimeError, match='interrupted'):
        tv.complete_launch('request', ['delivered'])
    tv.repository.complete_launch.assert_not_called()


def test_visual_publication_uses_the_bounded_sql_owner(tv):
    value = {'verdict': 'moving'}
    tv.publish_visual('visual00001', value, 100)
    tv.repository.replace_visual.assert_called_once_with('visual00001', value, 100, 240)
    tv.repository.transaction.assert_called_once()
