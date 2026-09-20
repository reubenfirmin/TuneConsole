import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import mode_surfaces as ms


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _eye(i, d=4):
    v = np.zeros(d, dtype=np.float32); v[i] = 1.0
    return v


def _seed(store, n_modes=4):
    store.modes.replace_modes(
        [{"mode_id": m, "label": f"m{m}", "families": [["house", 1]],
          "centroid": _eye(m - 1), "size": 80 - m, "rep_keys": []} for m in range(1, n_modes + 1)],
        retired_ids=[], now=1.0)

    def items(prefix):
        return [{"key": f"{prefix}{i}", "video_id": "v", "title": f"{prefix}{i}", "artist": f"{prefix}art{i}",
                 "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"}
                for i in range(20)]
    payload = {str(m): {} for m in range(1, n_modes + 1)}
    for surf in ms.CARD_SURFACES:
        for m in range(1, n_modes + 1):
            payload[str(m)][surf] = items(f"{surf}-m{m}-")
    payload["_meta"] = {"comfort_pool": 100, "year_cuts": None}   # comfort credible -> it holds the 4th slot
    store.put_proposals("mode_bundles", payload, 1.0)


def test_temporal_card_when_comfort_not_credible(store):
    store.modes.replace_modes(
        [{"mode_id": 1, "label": "m1", "families": [["house", 1]],
          "centroid": _eye(0), "size": 50, "rep_keys": []}], retired_ids=[], now=1.0)

    def t(prefix, year):
        return [{"key": f"{prefix}{i}", "video_id": "v", "title": f"{prefix}{i}", "artist": f"{prefix}a{i}",
                 "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "temporal",
                 "genre": "house", "year": year} for i in range(8)]
    # mode 1's temporal bucket spans old (1990) and new (2020); comfort empty + not credible
    payload = {"1": {"wheelhouse": [], "explore": [], "fresh": [], "comfort": [],
                     "temporal": t("old", 1990) + t("new", 2020)}}
    payload["_meta"] = {"comfort_pool": 0, "year_cuts": [2000, 2010]}     # comfort not credible
    store.put_proposals("mode_bundles", payload, 1.0)
    # band 0 (epoch 0) -> Throwback, only the <=2000 tracks
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    assert len(cards) == 1
    c = cards[0]
    assert c["lane"] == "temporal" and c["label"] == "Throwback"
    assert all(t["title"].startswith("old") for t in c["tracks"])
    # band 2 (epoch 2) -> Recent Picks, only the >2010 tracks
    c2 = ms.assemble_cards(store, now=10.0, epoch=2)[0]
    assert c2["label"] == "Recent Picks"
    assert all(t["title"].startswith("new") for t in c2["tracks"])


def test_comfort_not_backfilled(store):
    # Comfort credible (holds the slot) but its mode bucket is thin and there's a fat general pool.
    # Comfort must NOT pad from the general pool, so it stays thin / dropped - never faked.
    store.modes.replace_modes(
        [{"mode_id": 1, "label": "m1", "families": [["house", 1]],
          "centroid": _eye(0), "size": 50, "rep_keys": []}], retired_ids=[], now=1.0)
    thin = [{"key": f"c{i}", "video_id": "v", "title": "t", "artist": f"ca{i}", "album": "",
             "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"} for i in range(2)]
    fat = [{"key": f"g{i}", "video_id": "v", "title": "t", "artist": f"ga{i}", "album": "",
            "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"} for i in range(20)]
    payload = {"1": {"wheelhouse": [], "explore": [], "fresh": [], "comfort": thin},
               "all": {"comfort": fat}, "_meta": {"comfort_pool": 100, "year_cuts": None}}
    store.put_proposals("mode_bundles", payload, 1.0)
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    comfort = [c for c in cards if c["lane"] == "comfort"]
    # 2 thin tracks, < _MIN_CARD, and NO backfill -> comfort is dropped rather than padded from 'all'
    assert comfort == [] or all(not any(t["key"].startswith("g") for t in c["tracks"]) for c in comfort)


def test_comfort_thin_rotates_to_temporal(store):
    # Comfort holds the 4th slot (global pool credible) but its assigned mode's bucket is thin, and the
    # library has year data. Temporal must rotate into the slot so the row stays at four, not drop to 3.
    store.modes.replace_modes(
        [{"mode_id": m, "label": f"m{m}", "families": [["house", 1]],
          "centroid": _eye(m - 1), "size": 80 - m, "rep_keys": []} for m in range(1, 5)],
        retired_ids=[], now=1.0)

    def items(prefix, n, year=None):
        out = []
        for i in range(n):
            d = {"key": f"{prefix}{i}", "video_id": "v", "title": f"{prefix}{i}", "artist": f"{prefix}a{i}",
                 "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"}
            if year is not None:
                d["year"] = year
            out.append(d)
        return out

    # Slots 1-3 render on modes 1-3; mode 4's comfort bucket is thin (2) but its temporal bucket is deep.
    payload = {"1": {"wheelhouse": items("w", 20)},
               "2": {"explore": items("e", 20)},
               "3": {"fresh": items("f", 20)},
               "4": {"comfort": items("c", 2), "temporal": items("t", 8, year=1990)}}
    payload["_meta"] = {"comfort_pool": 100, "year_cuts": [2000, 2010]}   # comfort credible; year data present
    store.put_proposals("mode_bundles", payload, 1.0)

    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    lanes = [c["lane"] for c in cards]
    assert "comfort" not in lanes                 # thin comfort is not shown
    assert "temporal" in lanes                    # temporal rotated into the 4th slot
    assert len(cards) == 4                         # row stayed at four
    tcard = next(c for c in cards if c["lane"] == "temporal")
    assert tcard["mode_id"] == 4 and all(t["key"].startswith("t") for t in tcard["tracks"])


def test_assemble_four_cards_distinct_modes(store):
    _seed(store, n_modes=4)
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    assert len(cards) == 4
    assert len({c["mode_id"] for c in cards}) == 4          # one distinct mode per card
    assert all(c["mode_id"] in (1, 2, 3, 4) for c in cards)
    assert all(len(c["tracks"]) <= 12 for c in cards)
    keys = [t["key"] for c in cards for t in c["tracks"]]
    assert len(keys) == len(set(keys))                      # cross-card dedup
    assert all("mode_id" in c for c in cards)


def test_assemble_empty_without_bundles(store):
    assert ms.assemble_cards(store, now=10.0, epoch=0) == []


def test_full_buckets_do_not_pin_a_theme_to_one_framing(store):
    _seed(store)
    payload = store.get_proposals("mode_bundles")
    # One theme has the deepest pool in every framing. All the others can fill a
    # complete card too; raw pool depth must not undo the model's theme rotation.
    for mid in (2, 3, 4):
        for surf in ms.CARD_SURFACES:
            payload[str(mid)][surf] = payload[str(mid)][surf][:12]
    store.put_proposals("mode_bundles", payload, 1.0)
    menus = [ms.assemble_cards(store, now=10.0, epoch=e) for e in range(30)]
    wheelhouse = [next(c["mode_id"] for c in cards if c["lane"] == "wheelhouse") for cards in menus]
    assert len(set(wheelhouse)) >= 3
    assert all(len(cards) == 4 for cards in menus)
    assert all(len(c["tracks"]) == 12 for cards in menus for c in cards)


def test_logging_this_menu_does_not_change_its_themes_on_rerender(store):
    _seed(store)
    before = ms.assemble_cards(store, now=10.0, epoch=0)
    store.modes.log_impressions(0, [(c["lane"], c["mode_id"]) for c in before], 10.0)
    store.modes.log_pick(77, before[0]["mode_id"], 11.0)
    assert ms.assemble_cards(store, now=12.0, epoch=0) == before


@pytest.mark.parametrize("restricted_lane", ["comfort", "fresh"])
@pytest.mark.parametrize("available", [4, 12])
def test_assignment_preserves_the_only_mode_that_can_fill_a_lane(store, monkeypatch,
                                                               restricted_lane, available):
    _seed(store)
    payload = store.get_proposals("mode_bundles")
    # The preferred mode can fill any lane, but is the only source for this one.
    # Spending it on Wheelhouse loses a card, even when four cards are feasible.
    payload["1"][restricted_lane] = payload["1"][restricted_lane][:available]
    for mid in (2, 3, 4):
        payload[str(mid)][restricted_lane] = []
    store.put_proposals("mode_bundles", payload, 1.0)
    monkeypatch.setattr(ms, "select_modes", lambda *args, **kwargs: [1, 2, 3, 4])

    cards = ms.assemble_cards(store, now=10.0, epoch=0)

    assert len(cards) == 4
    assert len({c["mode_id"] for c in cards}) == 4
    restricted = next(c for c in cards if c["lane"] == restricted_lane)
    assert restricted["mode_id"] == 1
    assert len(restricted["tracks"]) == available


def test_diversify_caps_artist_and_album():
    items = (
        [{"artist": "A", "album": "AlbA", "key": f"a{i}"} for i in range(4)]        # same artist -> cap
        + [{"artist": f"B{i}", "album": "Comp", "key": f"c{i}"} for i in range(4)]  # one album -> cap
        + [{"artist": f"S{i}", "album": "", "key": f"s{i}"} for i in range(3)]      # singles -> uncapped
    )
    out = ms._diversify(items, max_artist=2, max_album=2)
    assert sum(1 for d in out if d["artist"] == "A") == 2
    assert sum(1 for d in out if d["album"] == "Comp") == 2
    assert sum(1 for d in out if d["album"] == "") == 3


def test_thin_bucket_backfilled_from_general_pool(store):
    store.modes.replace_modes(
        [{"mode_id": 1, "label": "m1", "families": [["house", 1]],
          "centroid": _eye(0), "size": 50, "rep_keys": []}], retired_ids=[], now=1.0)
    thin = [{"key": f"m{i}", "video_id": "v", "title": "t", "artist": f"mart{i}",
             "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"}
            for i in range(2)]                                  # only 2 in the mode bucket
    general = [{"key": f"g{i}", "video_id": "v", "title": "t", "artist": f"gart{i}",
               "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"}
               for i in range(20)]                              # general backfill pool
    payload = {"1": {}, "all": {}}
    for surf in ms.CARD_SURFACES:
        payload["1"][surf] = list(thin)
        payload["all"][surf] = list(general)
    store.put_proposals("mode_bundles", payload, 1.0)
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    assert len(cards) == 1                                      # one mode -> one card
    card = cards[0]
    assert len(card["tracks"]) == 12                           # backfilled from 2 up to PROTO_SIZE
    keys = {t["key"] for t in card["tracks"]}
    assert {"m0", "m1"} <= keys                                # the mode tracks lead
    assert any(k.startswith("g") for k in keys)                # backfilled from the general pool


def _genre_items(prefix, genre, count):
    return [{"key": f"{prefix}{i}", "video_id": f"v{prefix}{i}", "title": f"{prefix}{i}",
             "artist": f"{prefix} artist {i}", "album": "", "thumbnail": None, "genre": genre}
            for i in range(count)]


@pytest.mark.parametrize("prepared", [False, True])
def test_thin_mix_exhausts_deeper_family_before_adjacent_tracks(store, monkeypatch, prepared):
    from types import SimpleNamespace
    from yt_playlist.rec import embed, recommend

    store.modes.replace_modes([
        {"mode_id": 1, "label": "Soul", "families": [["soul-funk", 100]],
         "centroid": _eye(0), "size": 100, "rep_keys": []}], retired_ids=[], now=1.0)
    core = _genre_items("core", "Soul", 6)
    deep = _genre_items("deep", "Funk", 12)
    nearby = _genre_items("near", "Jazz", 20)
    unrelated = _genre_items("far", "Electronic", 20)
    payload = {"1": {"wheelhouse": core},
               "all": {"wheelhouse": unrelated + nearby + (deep if prepared else [])},
               "_meta": {"full_pools": prepared}}
    store.put_proposals("mode_bundles", payload, 1.0)
    store.record_dislike("deep0", until=None, now=1.0)
    monkeypatch.setattr(ms, "RecDao", lambda s: SimpleNamespace(generated_track_keys=lambda: {"deep1"}))

    def ranked_library(s, now, limit):
        assert not prepared  # full bundles serve without reranking
        assert limit >= 500
        return [SimpleNamespace(**d) for d in deep]

    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (list(range(500)), None, {}))
    monkeypatch.setattr(recommend, "for_you", ranked_library)

    cards = ms.assemble_cards(store, 10.0, 0, include_genres=["soul-funk", "electro-synth"])

    assert len(cards) == 1
    tracks = cards[0]["tracks"]
    assert len(tracks) == len({d["key"] for d in tracks}) == 12
    assert all(d["genre"] in {"Soul", "Funk"} for d in tracks)
    assert not {"deep0", "deep1"} & {d["key"] for d in tracks}
    assert store.get_proposals("mode_bundles") == payload


@pytest.mark.parametrize("lane", ["wheelhouse", "fresh"])
def test_thin_mix_uses_close_neighbors_but_never_avoided_or_unrelated_genres(store, lane):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "Soul", "families": [["soul-funk", 100]],
         "centroid": _eye(0), "size": 100, "rep_keys": []}], retired_ids=[], now=1.0)
    core = _genre_items("core", "Soul", 6)
    nearby = _genre_items("near", "Blues", 20)
    unrelated = _genre_items("far", "Techno", 20)
    avoided = _genre_items("avoid", "Jazz", 20)
    payload = {"1": {lane: core},
               "all": {lane: unrelated + avoided + nearby + core},
               "_meta": {"full_pools": True}}
    store.put_proposals("mode_bundles", payload, 1.0)

    cards = ms.assemble_cards(store, 10.0, 0, include_genres=["soul-funk"], avoid_genres=["jazz"])

    assert len(cards) == 1 and cards[0]["lane"] == lane
    tracks = cards[0]["tracks"]
    assert len(tracks) == len({d["key"] for d in tracks}) == 12
    assert [d["genre"] for d in tracks] == ["Soul"] * 6 + ["Blues"] * 6
