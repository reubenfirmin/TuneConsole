import sqlite3

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations import v031_spotify_history as upgrade
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.core import listening
from yt_playlist.core.db_revisions import Revisions


def _predecessor(path):
    store = Store(path)
    migrate(store.conn, registry=MIGRATIONS[:30])
    store._bind_repositories()
    iid = store.identities.upsert_identity('main', 'bridge', None, True)
    store.history.import_listening_events(iid, [('old-song',None,1700000000)], source='spotify')
    store.history.import_listening_events(iid, [('old-song',None,1700000000)], source='takeout')
    store.discovery.upsert_discovered_artist('Independent', 4, [], [], None, 100)
    return store


def _registry(apply=upgrade.apply):
    return (*MIGRATIONS[:30], Migration(31, 'spotify_history_provenance', apply))


def test_upgrade_keeps_event_ids_shared_sources_and_unknown_artist_origins(tmp_path):
    path = tmp_path / 'spotify.db'
    store = _predecessor(path)
    original = dict(store.conn.execute('SELECT * FROM play_events').fetchone())
    backups = set((tmp_path / 'backups').glob('*.db'))
    # Collision with the new spelling must retain one source, without losing Takeout.
    store.conn.execute("INSERT INTO play_event_sources VALUES (?,'spotify_export')", (original['id'],))
    store.conn.commit()
    migrate(store.conn, registry=_registry())
    upgrade.validate(store.conn)
    event = dict(store.conn.execute('SELECT * FROM play_events').fetchone())
    assert event == {**original, 'source': 'spotify_export'}
    assert {r[0] for r in store.conn.execute('SELECT source FROM play_event_sources')} == {'spotify_export','takeout'}
    assert store.conn.execute('SELECT source FROM discovery_artist_sources').fetchone()[0] == 'unknown'
    assert store.settings.get_setting('spotify_legacy_discovery_unattributed') == '1'
    new_backups = set((tmp_path / 'backups').glob('*.db')) - backups
    assert len(new_backups) == 1
    with sqlite3.connect(new_backups.pop()) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert backup.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 30
        assert backup.execute('SELECT id FROM play_events').fetchone()[0] == original['id']
    store.conn.close()
    reopened = Store(path)
    migrate(reopened.conn, registry=_registry())
    upgrade.validate(reopened.conn)
    assert reopened.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 1
    reopened.conn.close()


def test_interrupted_upgrade_rolls_back_source_rename_and_new_tables(tmp_path):
    store = _predecessor(tmp_path / 'interrupted.db')
    def fail(conn, context):
        upgrade.apply(conn, context)
        raise ValueError('injected failure')
    with pytest.raises(MigrationError, match='injected failure'):
        migrate(store.conn, registry=_registry(fail))
    assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 30
    assert not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_artist_sources'").fetchone()
    assert store.conn.execute('SELECT source FROM play_events').fetchone()[0] == 'spotify'
    migrate(store.conn, registry=_registry())
    upgrade.validate(store.conn)
    store.conn.close()


def test_daily_only_spotify_history_marks_unknown_legacy_influence():
    store = Store(':memory:')
    migrate(store.conn, registry=MIGRATIONS[:30])
    store._bind_repositories()
    iid = store.identities.upsert_identity('main', 'bridge', None, True)
    with store.conn:
        listening.calendar(store.conn, iid, 'America/Costa_Rica')
        listening.observe_day(store.conn, iid, 'old-song', '2024-01-01', 'spotify', 'UTC', 100)
    migrate(store.conn, registry=_registry())
    assert store.settings.get_setting('spotify_legacy_discovery_unattributed') == '1'
    # Install the runtime snapshot guard without upgrading this v031 fixture.
    store._snapshot_revisions = Revisions(store.conn)
    assert store.history.spotify_history_stats()['days'] == 0
    assert store.history.spotify_history_stats()['unresolved_days'] == 1
    store.conn.close()
