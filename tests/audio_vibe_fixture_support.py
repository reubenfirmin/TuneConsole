"""Private completed catalog input beneath individual specialist rollouts."""
from contextlib import closing

import pytest

from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator
from yt_playlist.core import schema_inspection
from yt_playlist.core.store import Store
from yt_playlist.rec import vibe_joint_state


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Individual model rollouts do not construct an HTTP application."""


@pytest.fixture
def specialist_store(tmp_path, monkeypatch, native_sql_calls):
    # The original subjects have one Psybient artist and two recordings.
    # Keep real SQL, file publication and all model refreshes. Snapshot races,
    # connection admission and joint-model fitting have their own lifecycle tests.
    monkeypatch.setattr(vibe_joint_state, 'QUALIFICATIONS', {})
    with bounded_consumer_schema((('psybient', 'Psybient'),), snapshot_guards=False) as schema:
        assert not schema.execute('PRAGMA foreign_key_check').fetchone()
        store = Store(tmp_path / 'specialist.db')
        with closing(store.conn) as conn, schema_inspection.reuse_tables(store.conn):
            # Set the disposable input's durability before its SQL copy commits.
            conn.execute('PRAGMA synchronous=OFF')
            initialize_consumer(store, schema, snapshot_guards=False)
            conn.execute('PRAGMA wal_autocheckpoint=0')
            with mock_snapshot_coordinator(store, monkeypatch):
                yield store


def qualify_related_predictions(monkeypatch, vibe):
    """Run the real related-recording model for one specialist in the fixture."""
    monkeypatch.setattr(vibe_joint_state, 'VIBES', (vibe,))
    monkeypatch.setattr(vibe_joint_state, 'QUALIFICATIONS',
                        {vibe_joint_state.config_identity(): 'controlled-fixture'})


def assert_only_playback_annotations(statements):
    """Permit capture's playback context without allowing rating or taxonomy reads."""
    assert not any('audio_taxonom' in sql for sql in statements)
    for sql in statements:
        if 'audio_annotation' not in sql:
            continue
        reads_playback = (sql.startswith('SELECT video_id,title,artist FROM audio_annotations WHERE run_key=')
                          and sql.endswith(" AND kind='playback'"))
        saves_playback = (sql.startswith('INSERT INTO audio_annotations(run_key,kind,video_id,title,artist,created_at)')
                          and ",'playback'," in sql)
        assert reads_playback or saves_playback or sql == 'UPDATE audio_annotations SET video_id=video_id WHERE 0', sql
