"""Complete application job outcomes over explicit collaborators, without SQLite."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.album_menu import AlbumNotesPreparation
from yt_playlist.controllers.mode_preparation import ModePreparation
from yt_playlist.controllers.pool_preparation import PoolBuilds
from yt_playlist.controllers.sync_execution import SyncExecution
from yt_playlist.core.activity import WorkClosing

pytestmark = pytest.mark.usefixtures('no_database')


def test_note_preparation_uses_the_current_menu_and_returns_pending_work():
    tuning = {'values': {'comfort': .6}, 'genres': ['jazz']}
    selected = [{'browse_id': 'visible'}]
    select = Mock(return_value=([], selected, {}))
    prepare = Mock(return_value=True)
    command = AlbumNotesPreparation(tuning=lambda now: tuning, epoch=lambda: 7,
        excluded=lambda epoch: ['previous'], current=lambda: 'playing', select=select, prepare=prepare)
    albums = [{'browse_id': 'candidate'}]
    assert command.prepare({'items': albums}, 100) is True
    select.assert_called_once_with(albums, 100, tuning['values'], 7, ['jazz'],
                                   exclude=['previous'], current='playing')
    prepare.assert_called_once_with(selected, 100)


@pytest.mark.parametrize('accepted', [False, True])
def test_mode_preparation_returns_the_payload_only_after_publication(accepted):
    snapshot = SimpleNamespace(store=object())
    publish = Mock(return_value=object() if accepted else None)
    payload = {'all': {'fresh': []}}
    policies = SimpleNamespace(rows={'p': 'policy'}, bases={'b': 'base'})
    settings = {'captured-inputs': 'revision'}
    capture = Mock(return_value=(payload, [], policies, settings))
    preparation = ModePreparation(maintain=Mock(), resume=lambda: (False, None),
        snapshot=lambda: nullcontext(snapshot), capture=capture, publish=publish)
    result = preparation.prepare(100)
    capture.assert_called_once_with(snapshot.store, 100)
    publish.assert_called_once_with(snapshot, payload, [], policies, settings, 100)
    assert result is (payload if accepted else None)


def test_cancelled_mode_capture_cannot_stage_or_publish():
    publish, snapshot = Mock(), SimpleNamespace(store=object())
    preparation = ModePreparation(maintain=Mock(), resume=lambda: (False, None),
        snapshot=lambda: nullcontext(snapshot), capture=Mock(side_effect=WorkClosing('stopped')),
        publish=publish)
    with pytest.raises(WorkClosing):
        preparation.prepare(100)
    publish.assert_not_called()


def test_deferred_mode_publication_reuses_captured_plan_without_recapturing():
    snapshot, capture = Mock(), Mock()
    payload = {'captured-generation': 'old-complete-inputs'}
    preparation = ModePreparation(maintain=Mock(), resume=lambda: (True, payload),
        snapshot=snapshot, capture=capture, publish=Mock())
    assert preparation.prepare(100) is payload
    snapshot.assert_not_called()
    capture.assert_not_called()


@pytest.mark.parametrize('waiting', [False, True])
def test_fresh_command_preserves_head_receipt_and_wakes_only_required_workers(waiting):
    operations = Mock()
    operations.client.return_value = 'client'
    operations.head.return_value = {'id': 7}
    operations.prepare.return_value = None if waiting else {'id': 8}
    command = PoolBuilds(client=operations.client, populate=operations.populate,
        prepare_fresh=operations.prepare, head=operations.head, prepare_library=operations.library,
        refresh_candidates=operations.candidates, refresh_home=operations.home, now=lambda: 100, logger=Mock())
    progress = Mock()
    result = command.fresh({'include': ['jazz']}, progress)
    assert result.result == {'generation': 7, 'requested_at': 100}
    assert result.outcome == ('waiting' if waiting else None)
    assert operations.mock_calls[:4] == [call.client(),
        call.populate('client', 100, include=['jazz'], on_progress=progress), call.head('fresh'), call.prepare(100)]
    assert operations.candidates.call_count == int(waiting)
    operations.home.assert_called_once_with(force=True)


@pytest.mark.parametrize('status', ['complete', 'partial', 'failed'])
def test_sync_outcome_is_complete_before_job_transport_receives_it(status):
    operations = Mock()
    operations.clients.return_value = {'account': 'client'}
    result = {'status': status, 'accounts': {}}
    operations.sync.return_value = result
    command = SyncExecution(begin=operations.begin, synchronize=operations.sync,
        clients=operations.clients, accounts=operations.accounts, finish=operations.finish,
        now=lambda: 100, wake=operations.wake)
    progress = Mock()
    outcome = command.run(progress, automatic=True, retry_now=True, max_age_s=20)
    assert outcome.result is result and outcome.outcome == status
    assert bool(outcome.error) == (status != 'complete')
    assert operations.mock_calls == [call.begin(100), call.clients(),
        call.sync({'account': 'client'}, 100, on_progress=progress, automatic=True, retry_now=True, max_age_s=20),
        call.wake()]


def test_unexpected_sync_failure_publishes_account_resources_before_waking_dependents():
    operations = Mock()
    operations.sync.side_effect = RuntimeError('offline')
    operations.clients.return_value = {}
    operations.accounts.return_value = [SimpleNamespace(id=4, label='Main')]
    operations.finish.side_effect = lambda result, now: result
    command = SyncExecution(begin=operations.begin, synchronize=operations.sync,
        clients=operations.clients, accounts=operations.accounts, finish=operations.finish,
        now=lambda: 100, wake=operations.wake)
    progress = Mock()
    outcome = command.run(progress)
    assert outcome.outcome == outcome.result['status'] == 'failed'
    account = outcome.result['accounts']['4']
    assert account['label'] == 'Main'
    assert set(account['resources']) == {'playlist_listing', 'playlists', 'ratings', 'history', 'saved_albums'}
    assert all(item['status'] == 'failed' for item in account['resources'].values())
    assert [entry[0] for entry in operations.mock_calls][-2:] == ['finish', 'wake']
    assert progress.call_args.args[0]['type'] == 'err'
