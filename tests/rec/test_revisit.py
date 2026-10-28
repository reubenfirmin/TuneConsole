"""Revisit is remembered music, with hard listening rests and per-visit variety."""
from tests.conftest import seed_catalog_track
from tests.rec.test_home_prepared import seed
from tests.test_recommend import _seed_album
from yt_playlist.library import ratings
from yt_playlist.rec import home_prepared, surfaces


DAY = 86400
NOW = 2000*DAY


def song(store, iid, name, *, plays=0, age=40, liked=False):
    seed_catalog_track(store, name, name, 'Artist', video_id='v-'+name)
    if liked:
        ratings.record_observations(store, iid, {'v-'+name: 'LIKE'}, source='sync', now=NOW-100)
    for i in range(plays):
        store.history.record_play_event(iid, name, 'v-'+name, NOW-age*DAY-i*1000, source_timezone='UTC')
    return name


def test_pool_needs_like_or_repeat_and_obeys_month_and_heavy_rotation_rest(store):
    iid = store.identities.upsert_identity('Main', 'cred', None, True)
    song(store, iid, 'never')
    song(store, iid, 'liked-unheard', liked=True)
    song(store, iid, 'one', plays=1)
    song(store, iid, 'repeated', plays=2)
    song(store, iid, 'recent', plays=2, age=29, liked=True)
    song(store, iid, 'heavy-too-soon', plays=20, age=180, liked=True)
    song(store, iid, 'heavy-rested', plays=20, age=184)
    out = surfaces.rediscover_tracks(store, NOW)
    assert [r['key'] for r in out] == ['liked-unheard', 'repeated', 'heavy-rested']
    assert [r['plays'] for r in out] == [0, 2, 20]


def test_generated_membership_is_eligible_but_a_real_recent_generated_play_counts_as_heard(store):
    iid = store.identities.upsert_identity('Main', 'cred', None, True)
    key = song(store, iid, 'favorite', plays=2, age=60)
    pid = store.playlists.upsert_playlist(iid, 'GEN', 'Generated', 1, None, NOW)
    tid = store.conn.execute('SELECT id FROM tracks WHERE video_id=?', ('v-favorite',)).fetchone()[0]
    store.playlists.set_playlist_tracks(pid, [tid])
    store.playlists.mark_generated_playlist('GEN')
    assert [r['key'] for r in surfaces.rediscover_tracks(store, NOW)] == [key]
    store.history.record_play_event(iid, key, 'v-favorite', NOW-DAY, playlist_ytm_id='GEN', source_timezone='UTC')
    assert surfaces.rediscover_tracks(store, NOW) == []


def test_current_rating_availability_and_new_play_revalidate_prepared_song_pool(store):
    iid = store.identities.upsert_identity('Main', 'cred', None, True)
    seed(store)
    keys = [song(store, iid, f'song{i}', plays=2, age=60) for i in range(20)]
    prepared = home_prepared.prepare(store, NOW)
    assert prepared and len(prepared['rediscover_tracks']) == 20
    first = home_prepared.collections(store, prepared, NOW)['rediscover_tracks']
    assert len(first) == 15
    assert home_prepared.collections(store, prepared, NOW)['rediscover_tracks'] == first
    store.rec_ledger.bump_card_view('rediscover', NOW)
    store.rec_ledger.bump_card_view('rediscover', NOW+1)
    following = home_prepared.collections(store, prepared, NOW+1)['rediscover_tracks']
    assert following != first
    ratings.record_confirmed(store, iid, 'v-song0', 'DISLIKE', NOW)
    store.history.record_play_event(iid, 'song1', 'v-song1', NOW, source_timezone='UTC')
    with store.conn:
        store.conn.execute('UPDATE catalog_tracks SET available=0 WHERE video_id=?', ('v-song2',))
    current = home_prepared.collections(store, prepared, NOW)['rediscover_tracks']
    assert not {'song0', 'song1', 'song2'} & {r['key'] for r in current}
    assert {r['key'] for r in current} <= set(keys)


def test_albums_never_claim_unheard_saved_collection_is_a_memory(store):
    iid = store.identities.upsert_identity('Main', 'cred', None, True)
    _seed_album(store, iid, 'unheard', 'Andre Sobota Collection', NOW)
    assert surfaces.rediscover_albums(store, NOW) == []
    _seed_album(store, iid, 'heard', 'Old favorite', NOW, played_at=NOW-60*DAY)
    assert [a['browse_id'] for a in surfaces.rediscover_albums(store, NOW)] == ['heard']


def test_each_component_advances_even_when_pool_fits_and_order_holds_during_reading():
    for component, count in [('albums', 3), ('playlists', 2), ('songs', 15)]:
        rows = list(range(count))
        first = surfaces.revisit_page(rows, count, 0, component)
        assert first == surfaces.revisit_page(rows, count, 0, component)
        assert first != surfaces.revisit_page(rows, count, 1, component)
        assert set(first) == set(rows)
