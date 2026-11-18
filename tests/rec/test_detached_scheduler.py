"""Scheduler policy over fake commands and a controlled clock, without SQLite."""
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec.progressive import SourceTurns
from yt_playlist.rec.worker_dispatch import PendingDispatch
from yt_playlist.rec.worker_queue import Job, PendingJobs

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def scheduler():
    jobs, clock = PendingJobs(), [100.]
    commands = Mock(spec_set=['retirement_pending', 'cleanup', 'cleanup_failed', 'evidence',
        'retention', 'playback', 'ratings', 'prepare', 'full_due', 'rebuild', 'modes', 'bundles',
        'projection', 'content_changed', 'home', 'poll_tv', 'choices', 'artwork', 'stories', 'tv', 'groups'])
    for name in ('retirement_pending', 'playback', 'prepare', 'home', 'choices', 'stories'):
        getattr(commands, name).return_value = False
    commands.ratings.return_value = set()
    commands.full_due.return_value = True
    commands.tv.return_value = False, False
    stop = Mock(spec_set=['wait'])
    # No wall-clock sleeps or background threads. Detect accidental busy loops.
    stop.wait.side_effect = [False] * 8 + [True]
    def schedule(**flags):
        for name, value in flags.items():
            if value:
                jobs.merge(Job(name))
    dispatch = PendingDispatch(jobs, commands, stop=stop, debounce=2, lock=RLock(), model_lock=RLock(),
        flush=Mock(), schedule=Mock(side_effect=schedule), active=Mock(), running=Mock(),
        clock=lambda: clock[0], logger=Mock())
    return SimpleNamespace(jobs=jobs, commands=commands, clock=clock, dispatch=dispatch)


def test_coalesces_full_and_modes_and_preserves_an_arrival_during_execution(scheduler):
    s = scheduler
    s.jobs.merge(Job.FULL)
    s.jobs.merge(Job.FULL)
    s.jobs.merge(Job.MODES)
    s.commands.rebuild.side_effect = lambda: s.jobs.merge(Job.RATINGS)
    s.dispatch.run()
    s.commands.rebuild.assert_called_once_with()
    s.commands.modes.assert_not_called()
    s.commands.ratings.assert_called_once_with()
    names = [entry[0] for entry in s.commands.mock_calls]
    assert names.index('cleanup') < names.index('rebuild') < names.index('ratings')
    assert not s.jobs
    s.dispatch.running.assert_called_with(False)


def test_cleanup_failure_retains_batch_and_new_arrivals_without_running_features(scheduler):
    s = scheduler
    s.jobs.merge(Job.CONTENT, {'first'})
    s.jobs.merge(Job.RETENTION)
    s.jobs.merge(Job.HOME)
    def fail(batch):
        s.jobs.merge(Job.CONTENT, {'second'})
        raise RuntimeError('cleanup interrupted')
    s.commands.cleanup.side_effect = fail
    s.dispatch.run()
    assert s.jobs.pending[Job.CONTENT] == {'first', 'second'}
    assert s.jobs.has(Job.HOME) and not s.jobs.has(Job.RETENTION)
    assert s.jobs.retry[Job.RETENTION].retry_at == 105
    s.commands.cleanup_failed.assert_called_once_with()
    s.commands.home.assert_not_called()
    s.commands.playback.assert_not_called()


def test_failed_full_fit_hands_mode_repair_back_without_replaying_full(scheduler):
    s = scheduler
    s.jobs.merge(Job.FULL)
    s.jobs.merge(Job.MODES)
    s.commands.rebuild.side_effect = RuntimeError('fit failed')
    s.dispatch.run()
    s.commands.rebuild.assert_called_once_with()
    s.commands.modes.assert_called_once_with()
    assert s.jobs.retry[Job.FULL].dirty and s.jobs.retry[Job.FULL].retry_at == 102


def test_full_backoff_keeps_ratings_responsive(scheduler):
    s = scheduler
    s.jobs.retry[Job.FULL].retry_at = 160
    s.jobs.merge(Job.FULL)
    s.jobs.merge(Job.RATINGS)
    s.dispatch.run()
    s.commands.rebuild.assert_not_called()
    s.commands.ratings.assert_called_once_with()
    assert s.jobs.retry[Job.FULL].dirty and s.jobs.retry[Job.FULL].retry_at == 160


@pytest.mark.parametrize('queued', [False, True])
def test_preparation_joins_one_bundle_turn(scheduler, queued):
    s = scheduler
    s.jobs.merge(Job.PREPARE)
    if queued:
        s.jobs.merge(Job.BUNDLES)
    s.commands.prepare.return_value = True
    s.dispatch.run()
    s.commands.bundles.assert_called_once_with()
    assert s.dispatch.schedule.call_count == (0 if queued else 1)


@pytest.mark.parametrize('failure,deadline', [(False, 105), (True, 160)])
def test_tv_fast_fill_does_not_override_provider_failure_backoff(scheduler, failure, deadline):
    s = scheduler
    s.jobs.merge(Job.TV)
    if failure:
        s.commands.tv.side_effect = RuntimeError('provider unavailable')
    else:
        s.commands.tv.return_value = True, True
    s.dispatch.run()
    assert s.jobs.retry[Job.TV].retry_at == deadline and s.jobs.retry[Job.TV].dirty


def test_cancellation_propagates_before_later_commands(scheduler):
    s = scheduler
    s.jobs.merge(Job.PREPARE)
    s.jobs.merge(Job.BUNDLES)
    s.jobs.merge(Job.HOME)
    s.commands.prepare.side_effect = WorkClosing('stopped')
    with pytest.raises(WorkClosing):
        s.dispatch.run()
    s.commands.bundles.assert_not_called()
    s.commands.home.assert_not_called()
    assert s.jobs.retry[Job.FULL].failures == 0


def source_commands():
    commands = Mock(spec_set=['capturing', 'advance', 'repair', 'joint_active', 'joint_scoring',
        'vibe', 'fanout', 'pending', 'start', 'musical', 'musical_pending'])
    for name in ('capturing', 'joint_active', 'joint_scoring', 'musical_pending'):
        getattr(commands, name).return_value = False
    commands.musical.return_value = True
    commands.vibe.return_value = True
    commands.pending.return_value = ['recording']
    return commands


def test_busy_sources_cannot_starve_recording_captures():
    commands, turns = source_commands(), SourceTurns()
    for _ in range(4):
        turns.capture(commands, 100)
    assert commands.start.call_args_list == [call(['recording'], 100)] * 2
    commands.musical.assert_called_once_with(100)
    commands.vibe.assert_called_once_with()


def test_joint_scoring_delivers_one_recording_batch_before_next_source_write():
    commands, turns = source_commands(), SourceTurns()
    commands.joint_scoring.return_value = True
    turns.capture(commands, 100)
    assert commands.mock_calls == [call.capturing(), call.repair(100), call.joint_active(),
        call.joint_scoring(), call.fanout(), call.pending(), call.start(['recording'], 100)]


def test_in_progress_capture_is_resumed_without_another_source_admission():
    commands, turns = source_commands(), SourceTurns()
    commands.capturing.return_value = True
    turns.capture(commands, 100)
    assert commands.mock_calls == [call.capturing(), call.advance()]
