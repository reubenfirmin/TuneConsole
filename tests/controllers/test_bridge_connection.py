"""Connection decisions require explicit capabilities, without a Store or SQLite."""
from contextlib import contextmanager, nullcontext
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.bridge_connection import BridgeConnection

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('paused', [False, True])
def test_reconciliation_uses_pause_guard_and_finishes_before_waking_sync(paused):
    events = []

    @contextmanager
    def guard():
        events.append('locked')
        try:
            yield
        finally:
            events.append('unlocked')

    settings = Mock(spec_set=['set_setting'])
    settings.set_setting.side_effect = lambda *args: events.append(('setting', args))
    command = BridgeConnection(settings, guard=guard(),
        paused=lambda: events.append('paused?') or paused,
        clear_authentication=lambda: events.append('clear'), wake=lambda: events.append('wake'))
    command.record()

    expected = ['locked', 'paused?']
    if not paused:
        expected.append(('setting', ('bridge_paired', '1')))
    assert events == [*expected, 'clear', 'unlocked', 'wake']
    assert settings.set_setting.call_count == int(not paused)


@pytest.mark.parametrize('failure', ['pairing', 'authentication'])
def test_unconfirmed_reconciliation_releases_guard_and_does_not_wake_sync(failure):
    events = []

    @contextmanager
    def guard():
        try:
            yield
        finally:
            events.append('unlocked')

    def reconcile(stage):
        events.append(stage)
        if stage == failure:
            raise RuntimeError('unconfirmed')

    settings = Mock(spec_set=['set_setting'])
    settings.set_setting.side_effect = lambda *args: reconcile('pairing')
    wake = Mock()
    command = BridgeConnection(settings, guard=guard(), paused=lambda: False,
        clear_authentication=lambda: reconcile('authentication'), wake=wake)
    with pytest.raises(RuntimeError, match='unconfirmed'):
        command.record()
    assert events == (['pairing', 'unlocked'] if failure == 'pairing'
                      else ['pairing', 'authentication', 'unlocked'])
    wake.assert_not_called()


def test_transport_without_pairing_storage_still_reconciles_authentication_and_wakes():
    events = []
    command = BridgeConnection(None, guard=nullcontext(), paused=Mock(),
        clear_authentication=lambda: events.append('clear'), wake=lambda: events.append('wake'))
    command.record()
    assert events == ['clear', 'wake']
    command.paused.assert_not_called()
