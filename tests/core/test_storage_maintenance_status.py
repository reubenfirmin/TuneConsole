"""Runtime maintenance notices compose repository results without opening storage."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yt_playlist.core.storage_maintenance import OPTIMIZING_BANNER, StorageMaintenance


def test_status_uses_repository_and_keeps_backlog_recording_and_copy_capture(monkeypatch):
    pending = Mock(side_effect=[3, 0, 2])

    class Store:
        playback_feedback = SimpleNamespace(pending=pending)

        @property
        def conn(self):
            pytest.fail('Maintenance status accessed the serving connection')

        @property
        def _playback_feedback(self):
            pytest.fail('Maintenance status accessed the physical playback inbox')

    def forbidden(*args, **kwargs):
        pytest.fail('Maintenance status opened SQLite outside its repository')

    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    maintenance = StorageMaintenance(Store())
    backlog = maintenance.status()
    assert backlog['pending_feedback'] == 3 and backlog['phase'] == 'playback_feedback'
    assert backlog['recording'] and backlog['capturing_feedback']
    assert not backlog['active'] and not backlog['warning'] and not backlog['banner_message']
    assert '3 saved playback observations' in backlog['message']

    drained = maintenance.status()
    assert drained['pending_feedback'] == 0 and drained['phase'] == 'idle'
    assert drained['recording'] and drained['capturing_feedback']
    assert not drained['message'] and not drained['banner_message']

    maintenance.active = True
    maintenance.phase = 'building'
    maintenance.detail = {'copied': 12}
    copying = maintenance.status()
    assert copying['active'] and not copying['recording'] and copying['capturing_feedback']
    assert copying['pending_feedback'] == 2 and copying['phase'] == 'building'
    assert copying['progress'] == {'copied': 12}
    assert copying['banner_message'] == OPTIMIZING_BANNER and not copying['warning']
    assert pending.call_count == 3
    assert all(not call.args and not call.kwargs for call in pending.call_args_list)


def test_priority_recommendation_cleanup_pauses_writes_without_copy_or_evidence():
    store = SimpleNamespace(playback_feedback=SimpleNamespace(pending=lambda: 2),
                            _recommendation_retirement_pending=True,
                            _recommendation_retirement_progress={'rows': 2048, 'pools': 3},
                            _recommendation_retirement_error='OperationalError')
    maintenance = StorageMaintenance(store)
    status = maintenance.status()
    assert status['active'] and status['phase'] == 'recommendation_retirement'
    assert 'Cleaning up old recommendations.' in status['message']
    assert not status['recording'] and status['capturing_feedback']
    assert status['progress'] == {'rows': 2048, 'pools': 3}
    assert status['error'] == 'OperationalError'
    store._recommendation_retirement_pending = False
    assert maintenance.status()['recording'] and not maintenance.status()['active']
