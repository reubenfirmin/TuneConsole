import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store


def discover(store, source, now=10):
    return store.discovery.upsert_discovered_track(
        'song|artist', 'video', 'Song', 'Artist', None, None, None, None, source, now)


def known_sources(store):
    store.discovery.upsert_discovered_album('album-a','Artist','Album',None,None,0)
    store.tracks.upsert_track('seed','Seed','Artist',None,20)


def test_rediscovery_keeps_all_sources_and_both_clocks(store):
    known_sources(store)
    key = discover(store, 'album-a')
    discover(store, 'radio:seed', 20)
    discover(store, 'genre:house', 30)
    discover(store, 'album-a', 40)
    sources = store.discovery.discovery_sources_for_track(key)
    assert [(s['source_type'], s['source_id'], s['found_at'], s['last_found_at'])
            for s in sources] == [('album', 'album-a', 10, 40),
                                  ('radio', 'seed', 20, 20), ('genre', 'house', 30, 30)]
    assert store.discovery.discovered_source_tokens('album') == {'album-a'}


def test_removal_keeps_other_sources_and_never_deletes_canonical_data(store):
    key = discover(store, 'album-a')
    discover(store, 'radio:seed')
    track = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    assert not store.discovery.remove_discovery_source(key, 'album-a')
    assert len(store.discovery.get_discovered_tracks()) == 1
    assert store.discovery.remove_discovery_source(key, 'radio:seed')
    assert store.discovery.get_discovered_tracks() == []
    assert store.tracks.catalog_subject(track)['video_id'] == 'video'
    assert store.conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_recent_source_and_generated_hold_protect_old_candidates(store):
    key = discover(store, 'album-a', 0)
    discover(store, 'radio:seed', 99 * 86400)
    assert store.discovery.gc_discovery_pool(100 * 86400, 30, set())['tracks'] == 0
    assert not store.discovery.remove_discovery_source(key, 'album-a')
    assert not store.discovery.remove_discovery_source(key, 'radio:seed', {key})
    assert len(store.discovery.get_discovered_tracks()) == 1
    assert store.discovery.gc_discovery_pool(100 * 86400, 30, {key})['tracks'] == 0
    assert store.discovery.gc_discovery_pool(100 * 86400, 30, set())['tracks'] == 1


def test_orphan_album_does_not_remove_radio_supported_candidate(store):
    store.tracks.upsert_track('seed','Seed','Artist',None,20)
    key = discover(store, 'gone-album')
    discover(store, 'radio:seed')
    assert store.discovery.prune_orphan_discovered_tracks() == 0
    assert store.discovery.remove_discovery_source(key, 'gone-album') is False
    assert store.discovery.discovery_sources_for_track(key)[0]['source_type'] == 'radio'


def test_populated_upgrade_reopen_and_failure_rollback_without_backups(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    migrate(store.conn, registry=MIGRATIONS[:17])
    store.conn.execute("INSERT INTO recordings(id,identity_key) VALUES (1,'legacy')")
    store.conn.execute("INSERT INTO discovery_sources VALUES (1,'radio:seed',NULL,12)")
    store.conn.execute("INSERT INTO discovery_sources VALUES (1,'album-a',NULL,NULL)")
    store.conn.commit()
    original = list(map(tuple, store.conn.execute('SELECT * FROM discovery_sources')))
    original_history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))

    def interrupted(conn, context):
        MIGRATIONS[17].apply(conn, context)
        raise RuntimeError('interrupted')

    with pytest.raises(MigrationError, match='interrupted'):
        migrate(store.conn, registry=(*MIGRATIONS[:17], Migration(18, 'typed_discovery_sources', interrupted)))
    assert list(map(tuple, store.conn.execute('SELECT * FROM discovery_sources'))) == original
    assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 17
    # The populated historical fixture explicitly opts into registered upgrades;
    # ordinary startup must keep refusing pre-floor bulk conversion.
    migrate(store.conn, registry=MIGRATIONS)
    store.init_schema()
    history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    assert history[:len(original_history)] == original_history
    rows = [dict(r) for r in store.conn.execute('SELECT s.*,o.legacy_source_key,o.resolution_status '
        'FROM discovery_sources s JOIN discovery_origins o ON o.id=s.origin_id ORDER BY o.legacy_source_key')]
    assert [(r['legacy_source_key'],r['resolution_status'],r['last_found_at']) for r in rows] == [
        ('album-a','unresolved',None),('radio:seed','unresolved',12)]
    store.conn.close()
    store = Store(path)
    store.init_schema()
    assert [dict(r) for r in store.conn.execute('SELECT s.*,o.legacy_source_key,o.resolution_status '
        'FROM discovery_sources s JOIN discovery_origins o ON o.id=s.origin_id ORDER BY o.legacy_source_key')] == rows
    assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
    assert not (tmp_path / 'backups').exists()
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    store.conn.close()
