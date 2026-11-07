"""TV policy checks over detached facts and mocked SQL persistence."""
from contextlib import nullcontext
from unittest.mock import Mock, create_autospec

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
    repo.pending_receipts.return_value = repo.pending_launches.return_value = 0
    return TVState(repo, Mock(), installed=True)


def test_invalid_visual_does_not_enter_sql(tv):
    with pytest.raises(ValueError):
        tv.publish_visual('visual00001', {'verdict': 'unsupported'}, 100)
    tv.repository.transaction.assert_not_called()
    tv.repository.replace_visual.assert_not_called()


@pytest.mark.parametrize('complete,expected', [(True, 7), (False, None)])
def test_artwork_completion_is_fenced_by_the_observed_demand(tv, complete, expected):
    tv.publish_artwork('artist', {'images': []}, 100, (7, 4), complete=complete)
    tv.repository.replace_artwork.assert_called_once_with('artist', {'images': []}, 100, (7, 4),
        request_to_complete=expected, limit=60)


@pytest.mark.parametrize('count,admitted', [(59, True), (60, False), (61, False)])
def test_pending_rollover_limit_does_not_erase_unconfirmed_intents(tv, count, admitted):
    tv.repository.pending_receipts.return_value = count
    if admitted:
        assert tv.begin_rollover('10:stream:request', 'next', 100)
        tv.repository.insert_receipt.assert_called_once_with('10:stream:request', (10, 'stream', 'request'), 'next', 100)
    else:
        with pytest.raises(ValueError, match='unconfirmed TV'):
            tv.begin_rollover('10:stream:request', 'next', 100)
        tv.repository.insert_receipt.assert_not_called()


def test_existing_rollover_is_not_created_again(tv):
    tv.repository.receipt_header.return_value = {'id': 8, 'status': 'pending', 'target_stream': 'next'}
    assert not tv.begin_rollover('10:stream:request', 'next', 100)
    tv.repository.insert_receipt.assert_not_called()


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


def test_legacy_visual_cache_retains_latest_240_without_sql(tv):
    tv.installed = False
    tv.proposals.get_proposals.return_value = {str(index): {'verdict': 'static'} for index in range(240)}
    tv.publish_visual('0', {'verdict': 'moving'}, 100)
    saved = tv.proposals.put_proposals.call_args.args[1]
    assert len(saved) == 240 and list(saved)[-1] == '0'
    assert saved['0'] == {'verdict': 'moving'}
    tv.repository.transaction.assert_not_called()
