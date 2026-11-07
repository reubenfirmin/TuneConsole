import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations import v031_spotify_history as upgrade
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from tests.historical_support import upgrade_fixture


def _predecessor(path):
    store = Store(path)
    migrate(store.conn, registry=MIGRATIONS[:30])
    # Populate the actual v30 owners; current repositories require later schemas.
    with store.conn:
        store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','bridge',1)")
        event = store.conn.execute("INSERT INTO play_events(identity_id,identity_key,raw_identity_key,played_at,source) "
                                   "VALUES (1,'old-song','old-song',1700000000,'spotify')").lastrowid
        store.conn.execute("INSERT INTO play_event_sources VALUES (?,'takeout')", (event,))
        artist = store.conn.execute("INSERT INTO artists(name,provisional) VALUES ('Independent',1)").lastrowid
        store.conn.execute("INSERT INTO discovered_artists(artist,artist_id,score,found_at) VALUES ('Independent',?,4,100)",
                           (artist,))
    store._bind_repositories()
    return store


def _registry(apply=upgrade.apply):
    return (*MIGRATIONS[:30], Migration(31, 'spotify_history_provenance', apply))


def test_upgrade_keeps_event_ids_shared_sources_and_unknown_artist_origins(tmp_path):
    path = tmp_path / 'spotify.db'
    store = _predecessor(path)
    original = dict(store.conn.execute('SELECT * FROM play_events').fetchone())
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
    assert not (tmp_path / 'backups').exists()
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
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
    with store.conn:
        store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','bridge',1)")
        store.conn.execute("INSERT INTO listening_calendars VALUES (1,'America/Costa_Rica',1)")
        store.conn.execute("INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,source_day,day,"
                           "source,timezone,precision,first_observed_at,last_observed_at) "
                           "VALUES (1,'old-song','old-song','2024-01-01',NULL,'spotify','UTC','calendar_day',100,100)")
    migrate(store.conn, registry=_registry())
    store._bind_repositories()
    assert store.settings.get_setting('spotify_legacy_discovery_unattributed') == '1'
    # Check today's statistics only after an explicit historical replay.
    upgrade_fixture(store)
    assert store.history.spotify_history_stats()['days'] == 0
    assert store.history.spotify_history_stats()['unresolved_days'] == 1
    store.conn.close()
