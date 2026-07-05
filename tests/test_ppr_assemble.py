import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import rec_params, mode_surfaces as ms


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _seed_one_mode(store):
    store.modes.replace_modes(
        [{"mode_id": 1, "label": "m1", "families": [["house", 1]],
          "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []}],
        retired_ids=[], now=1.0)
    # 6 distinct-artist wheelhouse candidates w0..w5 (>= _MIN_CARD after diversify; same genre so the
    # cosine arm's mood-tilt is uniform -> stable -> keeps bucket order w0..w5).
    items = [{"key": f"w{i}", "video_id": "v", "title": f"w{i}", "artist": f"art{i}",
              "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"}
             for i in range(6)]
    # PPR order reverses the bucket, so the 'ppr' arm must lead with w5.
    payload = {"1": {"wheelhouse": items, "explore": [], "comfort": [], "fresh": []},
               "_ppr": {"1": ["w5", "w4", "w3", "w2", "w1", "w0"]},
               "_meta": {"comfort_pool": 0, "year_cuts": None}}
    store.put_proposals("mode_bundles", payload, 1.0)


def test_ppr_arm_reorders_card(store):
    _seed_one_mode(store)
    rec_params.set_param(store, "ppr_ab_share", 1.0)      # every card -> ppr
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    wh = next(c for c in cards if c["lane"] == "wheelhouse")
    assert wh["ranker"] == "ppr"
    assert [t["key"] for t in wh["tracks"]] == ["w5", "w4", "w3", "w2", "w1", "w0"]


def test_cosine_arm_keeps_existing_order(store):
    _seed_one_mode(store)
    rec_params.set_param(store, "ppr_ab_share", 0.0)      # every card -> cosine (unchanged)
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    wh = next(c for c in cards if c["lane"] == "wheelhouse")
    assert wh["ranker"] == "cosine"
    assert [t["key"] for t in wh["tracks"]] == ["w0", "w1", "w2", "w3", "w4", "w5"]


def test_ranker_deterministic_per_epoch_mode(store):
    # _ranker_for is a pure function of (epoch, mode, share); same inputs -> same arm.
    assert ms._ranker_for(3, 7, 1.0) == ms._ranker_for(3, 7, 1.0) == "ppr"
    assert ms._ranker_for(3, 7, 0.0) == "cosine"


def test_ppr_arm_falls_back_to_cosine_when_ppr_pos_empty(store):
    # Coin flips 'ppr' (share=1.0), but this mode has no PPR data at all: the card must both order by
    # the true cosine path and be labeled honestly, not ship as 'ppr' with arbitrary bucket order.
    _seed_one_mode(store)
    bundles = store.get_proposals("mode_bundles")
    bundles["_ppr"] = {}                                   # no PPR data for mode 1
    store.put_proposals("mode_bundles", bundles, 1.0)
    rec_params.set_param(store, "ppr_ab_share", 1.0)       # coin would say 'ppr'
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    wh = next(c for c in cards if c["lane"] == "wheelhouse")
    assert wh["ranker"] == "cosine"
    assert [t["key"] for t in wh["tracks"]] == ["w0", "w1", "w2", "w3", "w4", "w5"]


def test_ppr_arm_partial_ranking_ranked_lead_then_bucket_order(store):
    # A partial PPR list: only some candidates are ranked. Ranked ones must lead in PPR order; the
    # unranked ones (all tied on the _PPR_TAIL sentinel) follow in their original bucket order. The
    # card stays labeled 'ppr' since PPR data does exist for this mode.
    _seed_one_mode(store)
    bundles = store.get_proposals("mode_bundles")
    bundles["_ppr"] = {"1": ["w5", "w3"]}                  # only w5, w3 ranked; w0,w1,w2,w4 unranked
    store.put_proposals("mode_bundles", bundles, 1.0)
    rec_params.set_param(store, "ppr_ab_share", 1.0)
    cards = ms.assemble_cards(store, now=10.0, epoch=0)
    wh = next(c for c in cards if c["lane"] == "wheelhouse")
    assert wh["ranker"] == "ppr"
    assert [t["key"] for t in wh["tracks"]] == ["w5", "w3", "w0", "w1", "w2", "w4"]
