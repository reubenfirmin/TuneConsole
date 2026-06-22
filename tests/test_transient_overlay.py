"""Tests for Task 8: transient facet leans wired into _axis_weights_for / explore_for_you."""
from yt_playlist import recommend
from yt_playlist.matching import identity_key


def test_axis_overlay_lowers_disfavored_facet(store):
    a = store.upsert_track("v1", "House One", "DJ A", None, None, 1)
    store.set_track_genre(a, "deep house")
    b = store.upsert_track("v2", "Jazz One", "Sax B", None, None, 1)
    store.set_track_genre(b, "jazz")
    ka, kb = identity_key("House One", "DJ A"), identity_key("Jazz One", "Sax B")
    store.record_mood([ka], -2, now=10.0)                      # "less" house, a lot
    mult = recommend._axis_weights_for(store, [ka, kb], now=10.0)
    assert mult is not None and mult[ka] < mult[kb]            # house ranked below jazz


def test_axis_weights_none_when_fully_neutral(store):
    a = store.upsert_track("v1", "T", "A", None, None, 1)
    k = identity_key("T", "A")
    assert recommend._axis_weights_for(store, [k], now=10.0) is None   # no weights, no leans


def test_explore_lane_respects_transient_facet_lean(store):
    from yt_playlist import embed
    iid = store.upsert_identity("main", "cred", None, True)
    house, jazz = [], []
    for i in range(6):
        h = store.upsert_track(f"h{i}", f"H{i}", f"DJ{i}", None, None); store.set_track_genre(h, "deep house"); house.append(h)
        j = store.upsert_track(f"j{i}", f"J{i}", f"Sx{i}", None, None); store.set_track_genre(j, "jazz"); jazz.append(j)
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 12, "h", 0.0), house + jazz)
    embed.build_and_store(store, dim=4)                     # neither artist is "familiar" (no plays)
    for i in range(6):
        store.record_mood([identity_key(f"H{i}", f"DJ{i}")], -2, now=10.0)   # "less house", a lot
    ex = recommend.explore_for_you(store, now=10.0, limit=12)
    assert ex and ex[0].artist.startswith("Sx")            # jazz tops; house is disfavored right now
