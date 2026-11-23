from dataclasses import replace

import pytest
from tests.derived_rating_support import seed_dislike
from tests.group_support import publish, publish_keyed, recordings
from yt_playlist.rec import group_discovery, home_vibe, surfaces, mode_surfaces as ms
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_tuning import DEFAULTS
from yt_playlist.rec.vibe_pool_contract import GENRE_RESERVE, POOL_LIMIT
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, native_sql_calls, uncontended_file_consumer_store as store,
)


pytestmark = pytest.mark.usefixtures('native_sql_calls')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    # Pool preparation does not create the web app or its sync worker.
    pass


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'), ('techno', 'Techno')),
                                 snapshot_guards=False) as schema:
        yield schema


def seed_items(store, keys, genre='House'):
    seed_publication_subjects(store, [dict(key=key, title='T' + key, artist='A',
        video_id='v' + key, genre=genre, year='2000') for key in keys])


class _Item:
    def __init__(self, key, score):
        self.key = key; self.video_id = "v"+key; self.title = "T"+key; self.artist = "A"
        self.album = ""; self.thumbnail = None; self.plays = 0; self.reason = ""; self.lane = ""
        self.genre = "house"; self._score = score


def test_prepare_buckets_by_published_membership(monkeypatch, store):
    seed_items(store, ['k1'])
    seed_items(store, ['k2'], 'Techno')
    membership = publish_keyed(store, [('House', ['k1'], .1), ('Techno', ['k2'], .9)])
    pool = [_Item("k1", 0.9), _Item("k2", 0.8)]
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(surfaces, fn, lambda s, n, limit=0, _p=pool, **kw: list(_p))
    fresh = surfaces.ForYouItem("New song", "New artist", "", "new", None, 0, "", key="new|artist",
                                genre="house", year=2026)
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None, **kw: [fresh])
    store.discovery.upsert_discovered_track(fresh.key, fresh.video_id, fresh.title, fresh.artist,
                                            '', None, 'House', '2026', None, 1)
    publish_bundles(store, {}, 1, fresh=[{'key': 'stale|artist', 'genre': 'Techno'}])

    payload = ms.prepare_bundles(store, now=10.0)
    assert [d["key"] for d in payload[membership['k1']]["wheelhouse"]] == ["k1"]
    assert [d["key"] for d in payload[membership['k2']]["wheelhouse"]] == ["k2"]
    assert payload["all"]["fresh"][0]["year"] == 2026
    assert store.recommendations.head('home') is not None
    refreshed = store.recommendations.candidate_page('fresh', 'all', 'fresh')['tracks']
    assert len(refreshed) == 1 and refreshed[0]["key"] == "new|artist"
    assert refreshed[0]["genre"] == "House" and refreshed[0]["year"] == 2026
    assert refreshed[0]["score"] is None
    assert store.rec_ledger.policy_configuration(refreshed[0]['pool_policy']) is not None


def test_worker_publication_keeps_discoveries_in_grouped_on_air_rotation(store, monkeypatch):
    from tests.radio_session_support import play, radio_controller
    from yt_playlist.library import generated_acquisition
    from yt_playlist.web import radio_session

    now = 1_700_000_000
    tracks = [dict(key=key, title=key, artist=f'Artist {key}', video_id=f'v-{key}',
                   genre=genre, year='2024', duration_s=200) for key, genre in (
                       ('seed', 'House'), ('owned', 'House'), ('owned-techno', 'Techno'),
                       ('blocked', 'House'), ('new-one', 'House'), ('new-two', 'House'),
                       ('new-techno', 'Techno'))]
    seed_publication_subjects(store, tracks, discoveries={'blocked', 'new-one', 'new-two', 'new-techno'}, now=now)
    membership = publish_keyed(store, [('House', ['seed', 'owned'], .1), ('Techno', ['owned-techno'], .9)])
    payload = ms.prepare_bundles(store, now)
    assert payload[membership['seed']]['fresh'] == []
    assert {t['key'] for t in store.recommendations.candidate_page('fresh', 'all', 'fresh')['tracks']} == {
        'blocked', 'new-one', 'new-two', 'new-techno'}

    # Apply a current dislike after publication. Also exclude one exact
    # upload and keep both the genre pin and the Discovery allocation.
    seed_dislike(store, 'blocked', until=None, now=now + 1)
    for energy in (.5, .6):
        candidates = RadioCandidates(store, now + 2, {**DEFAULTS, 'comfort': 0., 'theme': 1., 'energy': energy})
        try:
            seed = {**tracks[0], '_radio_rotation_slot': 'unplayed'}
            next_track = candidates.next_track([seed], excluded={'v-new-one'})
            assert (next_track['key'], next_track['_radio_rotation']) == ('new-two', 'new')
        finally:
            candidates.close()

    create = generated_acquisition.create_generated_playlist
    with radio_controller(monkeypatch, store, tracks) as (controller, remote, _, _, _):
        monkeypatch.setattr(radio_session, 'RadioCandidates', RadioCandidates)
        monkeypatch.setattr(generated_acquisition, 'create_generated_playlist', create)
        remote.get_song = lambda _: {'videoDetails': {'lengthSeconds': '200'}}
        controller.ctx.now_fn = lambda: now + 2
        started = controller.start(seed_video_id='v-seed')
        assert started['ok'], started
        assert len(remote.creations) == 1
        assert remote.ids == ['v-seed']
        assert not remote.adds

        play(controller, 'v-seed')
        assert any(track['_radio_rotation_slot'] == 'new' for track in controller.session.tracks)
        for video in remote.ids[1:]:
            play(controller, video)
        assert len(remote.ids) > 3
        assert 'v-blocked' not in remote.ids


def test_worker_publication_continues_themed_from_singleton_into_same_genre(store):
    now = 1_700_000_000
    tracks = [dict(key=key, title=key, artist=f'Artist {key}', video_id=f'v-{key}',
                   genre=genre, year='2024', duration_s=200) for key, genre in (
                       ('singleton', 'House'), ('other-one', 'House'), ('other-two', 'House'), ('techno', 'Techno'))]
    seed_publication_subjects(store, tracks)
    music = recordings([('House', 1, 0.), ('House', 2, 1.), ('Techno', 1, 0.)])
    head = publish(store, [replace(track, key=row['key'], year=2024) for track, row in zip(music, tracks, strict=True)],
                   policy=group_discovery.Policy(style_weight=.35, broad_edge=.86, fine_edge=.9, detail_edge=.95))
    members = store.taste_groups.leaf_members(head['generation'])
    assert members['other-one'] == members['other-two'] != members['singleton']
    ms.prepare_bundles(store, now)

    candidates = RadioCandidates(store, now + 1, {**DEFAULTS, 'theme': 1.})
    try:
        seed = tracks[0]
        anchor = candidates.direction_anchor([seed], [])
        chosen = candidates.next_track([seed], excluded={anchor['video_id']})
        assert {anchor['key'], chosen['key']} == {'other-one', 'other-two'}
        assert candidates.fits(chosen, seed)
        assert candidates.next_track([seed, anchor, chosen]) is None
    finally:
        candidates.close()


def test_temporal_excludes_disliked_library_tracks(monkeypatch, store):
    """The temporal surface is built straight from library content-vector membership, so it must still
    honour YouTube dislikes (suppressed_keys) the way the scorer pools do, or a thumbs-down track leaks
    into the Throwback card and into the generated playlist."""
    keys = ["keep", "hated"]
    seed_items(store, keys)
    membership = publish_keyed(store, [('House', keys, .1)])
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(surfaces, fn, lambda s, n, limit=0, **kw: [])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None, **kw: [])
    seed_dislike(store, "hated", until=None, now=10.0)

    payload = ms.prepare_bundles(store, now=10.0)
    temporal_keys = {d["key"] for d in payload[membership['keep']]["temporal"]}
    all_temporal_keys = {d["key"] for d in payload["all"]["temporal"]}
    assert "keep" in temporal_keys
    assert "hated" not in temporal_keys
    assert "hated" not in all_temporal_keys


def test_prepare_empty_when_no_modes(store):
    assert store.recommendations.head('home') is None  # Unprepared is distinct from a completed empty output.
    assert ms.prepare_bundles(store, now=10.0) == {}
    assert store.recommendations.head('home') is not None
    assert store.recommendations.framing('home')['presence'] == 0
    assert store.recommendations.selections('home', 'wheelhouse') == []


def test_fresh_refresh_discards_pools_without_live_models_or_candidates(store):
    from yt_playlist.rec import fresh_pool

    old = [{"key": "stale", "title": "Stale", "artist": "Artist", "genre": "House"}]
    library = [{"key": "owned", "title": "Owned", "artist": "Library artist", "genre": "House"}]
    publish_bundles(store, {
        "1": {"fresh": old, "wheelhouse": library},
        "all": {"fresh": old, "wheelhouse": library},
        "_meta": {"full_pools": True}, "_ppr": {"1": ["owned"]},
    }, 1, fresh=old)

    fresh_pool.prepare(store, 10)  # No discoveries remain.

    assert fresh_pool.pick(store, 10) == []
    assert store.recommendations.framing('home')['presence'] == 0
    assert store.recommendations.candidate_page('fresh', 'all', 'fresh')['tracks'] == []


@pytest.fixture
def large_ranked_catalog(monkeypatch, store):
    keys = [f"k{i}" for i in range(500)]
    seed_items(store, keys[:450])
    seed_items(store, keys[450:], 'Techno')
    membership = publish_keyed(store, [('House', keys[:450], .1), ('Techno', keys[450:], .9)])
    pool = [_Item(key, 1.0) for key in keys]
    for track in pool[450:]:
        track.genre = 'techno'
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(surfaces, fn, lambda s, n, limit, **kw: pool[:limit])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None, **kw: [])
    return membership[keys[-1]]


def test_prepare_reaches_genres_below_the_old_global_pool_cutoff(
        monkeypatch, store, large_ranked_catalog):
    # Imported songs have no prepared musical profiles. Keep every genre
    # reserve and typed publication while supplying this lower input directly.
    monkeypatch.setattr(ms.vibe_pools.musical_character, 'load', lambda reader, keys: {})

    payload = ms.prepare_bundles(store, now=10.0)

    assert len(payload[large_ranked_catalog]["wheelhouse"]) >= ms.PROTO_SIZE
    assert len(payload["all"]["wheelhouse"]) == 500
    assert len(payload["all"]["temporal"]) == 500
    assert {item['genre'] for item in payload['all']['wheelhouse']} == {'house', 'techno'}
    assert {item['genre'] for item in payload['all']['temporal']} == {'House', 'Techno'}


    for preset in home_vibe.PRESETS:
        for lane, label in (('wheelhouse', 'house'), ('explore', 'house'),
                            ('comfort', 'house'), ('temporal:0', 'House')):
            reserve = payload['_vibes']['genres'][preset][lane]['house']
            assert 0 < len(reserve) <= GENRE_RESERVE
            assert len({item['key'] for item in reserve}) == len(reserve)
            assert all(item['genre'] == label for item in reserve)
            assert len(payload['_vibes']['pools'][preset][lane]) <= POOL_LIMIT
            assert store.recommendations.pool('vibe:' + preset, 'house', lane) is not None
