"""Private completed catalog input beneath individual specialist rollouts."""
from contextlib import closing

import pytest

from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator
from yt_playlist.core.store import Store
from yt_playlist.rec import vibe_joint_preparation


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Individual model rollouts do not construct an HTTP application."""


@pytest.fixture
def specialist_store(tmp_path, monkeypatch):
    # The original subjects have one Psybient artist and two recordings.
    # Keep real SQL, file publication and all model refreshes. Snapshot races
    # and joint-model fitting have their own lifecycle tests.
    monkeypatch.setattr(vibe_joint_preparation, 'qualified', lambda: False)
    with bounded_consumer_schema((('psybient', 'Psybient'),), snapshot_guards=False) as schema:
        assert not schema.execute('PRAGMA foreign_key_check').fetchone()
        store = Store(tmp_path / 'specialist.db')
        with closing(store.conn) as conn:
            initialize_consumer(store, schema, snapshot_guards=False)
            conn.execute('PRAGMA synchronous=OFF')
            conn.execute('PRAGMA wal_autocheckpoint=0')
            with mock_snapshot_coordinator(store, monkeypatch):
                yield store
