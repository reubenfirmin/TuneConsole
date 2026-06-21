from yt_playlist import recommend
from yt_playlist.rec_dao import RecDao


def test_eroded_after_view_cap_then_recycled(store):
    store.upsert_identity("main", "cred", None, True)
    dao = RecDao(store)
    for t in (0.0, 400.0, 800.0):                 # 3 spaced views (beyond debounce)
        dao.record_impressions("for_you", ["a|b"], now=t, debounce_s=300)
    assert "a|b" in dao.eroded_keys("for_you", now=900.0, view_cap=3, cooldown_days=14)
    # after the cooldown it recycles back in
    assert "a|b" not in dao.eroded_keys("for_you", now=900.0 + 15 * 86400, view_cap=3, cooldown_days=14)


def test_debounce_prevents_multicount(store):
    store.upsert_identity("main", "cred", None, True)
    dao = RecDao(store)
    for _ in range(5):                            # rapid re-shows at the same instant
        dao.record_impressions("for_you", ["x|y"], now=100.0, debounce_s=300)
    assert "x|y" not in dao.eroded_keys("for_you", now=200.0, view_cap=3)   # counted once, not 5x


def test_for_you_hides_eroded_items(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Hit", "Fav", None, None)
    store.upsert_track("v2", "Bench", "Fav", None, None)   # deep cut -> in for_you
    now = 1000.0
    store.add_history_snapshot(iid, now - 100, ["hit|fav"])
    store.add_history_snapshot(iid, now - 50, ["hit|fav"])
    assert "bench|fav" in {i.key for i in recommend.for_you(store, now=now)}

    dao = RecDao(store)
    for t in (0.0, 400.0, 800.0):
        dao.record_impressions("for_you", ["bench|fav"], now=t, debounce_s=300)
    assert "bench|fav" not in {i.key for i in recommend.for_you(store, now=900.0)}
