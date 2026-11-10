from contextlib import contextmanager, nullcontext
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.enrichment_state import EnrichmentState
from yt_playlist.controllers.retention import Retention
from yt_playlist.controllers.vibe_models import VibeModels
from yt_playlist.core.maintenance_context import MaintenanceDatabase

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def retention():
    database = MaintenanceDatabase(lambda: 'connection', nullcontext(), None, lambda: False)
    return Retention(database, create_autospec(EnrichmentState, instance=True, spec_set=True),
                     create_autospec(VibeModels, instance=True, spec_set=True))


def test_managed_collection_expires_alternatives_before_scanning(retention, monkeypatch):
    from yt_playlist.core.preparation import managed_files

    calls = []
    retention.enrichment.expire_provider_alternatives.side_effect = lambda now: calls.append(('expire', now))
    collect = Mock(side_effect=lambda database, now: calls.append(('collect', database, now)) or 3)
    monkeypatch.setattr(managed_files, 'maintain', collect)
    assert retention.maintain_managed_files(123) == 3
    assert calls == [('expire', 123), ('collect', retention.database, 123)]

    retention.enrichment.expire_provider_alternatives.side_effect = OSError('interrupted expiry')
    with pytest.raises(OSError, match='interrupted expiry'):
        retention.maintain_managed_files(124)
    collect.assert_called_once_with(retention.database, 123)


@pytest.mark.parametrize('available', [False, True])
def test_prediction_expiry_commits_before_history_and_file_collection(retention, monkeypatch, available):
    from yt_playlist.core import db_writes, vibe_history, vibe_provenance
    from yt_playlist.repos import vibe_models

    calls = []

    @contextmanager
    def write(database):
        assert database is retention.database
        calls.append('begin')
        yield
        calls.append('commit')

    monkeypatch.setattr(db_writes, 'atomic_write', write)
    monkeypatch.setattr(vibe_provenance, 'available', lambda conn: available)
    monkeypatch.setattr(vibe_provenance, 'maintain', lambda conn, now: calls.append('expire') or 2)
    monkeypatch.setattr(vibe_history, 'maintain', lambda database, now: calls.append('history') or 3)
    monkeypatch.setattr(vibe_provenance, 'collect', lambda conn, now: calls.append('files'))
    monkeypatch.setattr(vibe_models, 'available', lambda conn: True)
    retention.models.collect.side_effect = lambda now: calls.append('models')

    assert retention.maintain_prediction_provenance(123) == (5 if available else 0)
    assert calls == (['begin', 'expire', 'commit', 'history', 'files', 'models'] if available else [])


def test_shutdown_releases_both_disposable_iterators(retention, monkeypatch):
    from yt_playlist.core import collection_recovery
    from yt_playlist.core.preparation import managed_files

    managed, recovery = Mock(), Mock()
    monkeypatch.setattr(managed_files, 'close', managed)
    monkeypatch.setattr(collection_recovery, 'close', recovery)
    retention.close()
    managed.assert_called_once_with(retention.database)
    recovery.assert_called_once_with(retention.database)


def test_database_context_follows_replaced_connection_and_current_pause_state():
    current, paused = ['first'], [False]
    database = MaintenanceDatabase(lambda: current[0], nullcontext(), None, lambda: paused[0])
    assert database.conn == 'first' and not database.activity_paused
    current[0], paused[0] = 'replacement', True
    assert database.conn == 'replacement' and database.activity_paused
    with pytest.raises(AttributeError):
        database.settings
