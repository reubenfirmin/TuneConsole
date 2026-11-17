"""Detached release decisions and restart admission; opening SQLite is forbidden."""
import asyncio
import logging
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.updates import UpdateChecks, UpdateCommands
from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.core.update_lifecycle import PendingRestart, acquire_restart_locks
from yt_playlist.core.update_rules import check_due, update_offer

pytestmark = pytest.mark.usefixtures('no_database')


def settings(values=None):
    values = dict(values or {})
    return NS(get_setting=lambda key: values.get(key),
              set_setting=lambda key, value: values.__setitem__(key, value), values=values)


@pytest.mark.parametrize('last,deadline,now,due', [
    (None, '110', 109, False), (None, '110', 110, True), ('100', '999', 109, False),
    ('100', None, 110, True), ('bad', None, 0, True), (None, 'bad', 0, True),
])
def test_due_uses_persisted_first_deadline_and_then_last_attempt(last, deadline, now, due):
    assert check_due(last, deadline, now, 10) is due


def test_failed_release_check_keeps_content_and_throttles_retry():
    saved = settings({'latest_version_seen': '1.2.0'})
    fetch = Mock(side_effect=OSError('offline'))
    owner = UpdateChecks(saved, fetch, current_version=lambda: '1.0', install_kind=lambda: 'release')
    owner.check(100, interval_s=10)
    owner.check(109, interval_s=10)
    assert fetch.call_count == 1
    assert saved.values == {'latest_version_seen': '1.2.0', 'latest_version_checked_at': '100'}
    assert owner.offer() == {'current': '1.0', 'latest': '1.2.0', 'kind': 'release'}


def test_cancelled_release_check_never_marks_attempt_complete():
    saved = settings()
    owner = UpdateChecks(saved, Mock(side_effect=WorkClosing()),
                         current_version=Mock(), install_kind=Mock())
    with pytest.raises(WorkClosing):
        owner.check(100)
    assert saved.values == {}


def test_update_offer_suppresses_checkout_and_exact_dismissal_without_labels():
    assert update_offer('1.0', '2.0', None, 'dev') is None
    assert update_offer('1.0', '2.0', '2.0', 'flatpak') is None
    assert update_offer('bad', '2.0', None, 'flatpak') is None
    assert update_offer('1.0', '2.1', '2.0', 'flatpak') == {
        'current': '1.0', 'latest': '2.1', 'kind': 'flatpak'}


def test_job_inspection_failure_releases_both_admission_locks():
    work = Activity()
    locks = [threading.Lock(), threading.Lock()]
    with pytest.raises(RuntimeError):
        acquire_restart_locks(work, locks, NS(has_active=Mock(side_effect=RuntimeError())))
    assert all(not lock.locked() for lock in locks)


def test_restart_cancel_before_first_task_turn_preserves_shutdown_admission():
    async def exercise():
        work = Activity()
        locks = [threading.Lock(), threading.Lock()]
        updater = NS(phase='ready', busy=False)
        pending = PendingRestart(work, updater, locks, NS(has_active=lambda: False),
                                 logging.getLogger(__name__), Mock())
        pending.request('localhost', 8765)
        work.close_admission('shutdown')
        assert await pending.cancel()
        assert not work.accepting and work.closing_reason == 'shutdown'
        assert pending.phase is None and all(not lock.locked() for lock in locks)
    asyncio.run(exercise())


def test_restart_address_and_ready_state_are_validated_before_admission():
    pending = Mock(phase=None)
    updater = NS(phase='ready')
    commands = UpdateCommands(updater, pending)
    with pytest.raises(ValueError, match='local app'):
        commands.restart('external.example', 8765)
    updater.phase = 'available'
    with pytest.raises(ValueError, match='Install'):
        commands.restart('localhost', 8765)
    pending.request.assert_not_called()
