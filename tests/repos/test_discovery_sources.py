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


def test_recent_source_and_generated_hold_protect_old_candidates(store):
    key = discover(store, 'album-a', 0)
    discover(store, 'radio:seed', 99 * 86400)
    track = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    sources = store.discovery.discovery_sources_for_track(key)
    assert {row['source_token'] for row in sources} == {'album-a', 'radio:seed'}
    assert store.discovery.gc_discovery_pool(100 * 86400, 30, set())['tracks'] == 0
    assert store.discovery.discovery_sources_for_track(key) == sources
    # The hold must protect the candidate after both sources have aged out.
    assert store.discovery.gc_discovery_pool(131 * 86400, 30, {key})['tracks'] == 0
    assert len(store.discovery.get_discovered_tracks()) == 1
    assert store.discovery.gc_discovery_pool(131 * 86400, 30, set())['tracks'] == 1
    assert store.discovery.get_discovered_tracks() == []
    assert store.discovery.discovery_sources_for_track(key) == sources
    assert store.tracks.catalog_subject(track)['video_id'] == 'video'
    assert store.conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_orphan_album_does_not_remove_radio_supported_candidate(store):
    store.tracks.upsert_track('seed','Seed','Artist',None,20)
    store.discovery.upsert_discovered_album('gone-album', 'Artist', 'Album', None, None, 0)
    key = discover(store, 'gone-album')
    discover(store, 'radio:seed')
    store.discovery.delete_discovered_albums(['gone-album'])
    assert store.discovery.prune_orphan_discovered_tracks() == 0
    assert set(store.discovery.discovered_tracks_by_keys([key])) == {key}
    assert {row['source_type'] for row in store.discovery.discovery_sources_for_track(key)} == {'album', 'radio'}
