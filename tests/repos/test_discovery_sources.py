import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store


def discover(store, source, now=10):
    return store.discovery.upsert_discovered_track(
        'song|artist', 'video', 'Song', 'Artist', None, None, None, None, source, now)


def test_rediscovery_keeps_all_sources_and_both_clocks(store):
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
    store.rec_proposals.put_proposals('fresh_pool', [{'key': key}], 1)
    assert not store.discovery.remove_discovery_source(key, 'album-a')
    assert len(store.discovery.get_discovered_tracks()) == 1
    assert store.discovery.remove_discovery_source(key, 'radio:seed')
    assert store.discovery.get_discovered_tracks() == []
    assert store.rec_proposals.get_proposals('fresh_pool') == []
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

    def interrupted(conn, context):
        MIGRATIONS[17].apply(conn, context)
        raise RuntimeError('interrupted')

    with pytest.raises(MigrationError, match='interrupted'):
        migrate(store.conn, registry=(*MIGRATIONS[:17], Migration(18, 'typed_discovery_sources', interrupted)))
    assert list(map(tuple, store.conn.execute('SELECT * FROM discovery_sources'))) == original
    assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 17
    store.init_schema()
    rows = [dict(r) for r in store.conn.execute('SELECT * FROM discovery_sources ORDER BY source_token')]
    assert [(r['source_type'], r['source_id'], r['last_found_at']) for r in rows] == [
        ('album', 'album-a', None), ('radio', 'seed', 12)]
    store.init_schema()
    assert [dict(r) for r in store.conn.execute('SELECT * FROM discovery_sources ORDER BY source_token')] == rows
    assert not (tmp_path / 'backups').exists()
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
