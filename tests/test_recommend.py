from yt_playlist import recommend
from yt_playlist.store import Store


def test_resurface_picks_played_but_not_recent(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Gem", "X", None, None)      # key "gem|x"
    store.upsert_track("v2", "Fresh", "X", None, None)    # key "fresh|x"
    day = 86400.0
    now = 200 * day
    # "Gem": two old snapshots (plays=2), last play 119 days ago -> outside the 90d window
    store.add_history_snapshot(iid, now - 120 * day, ["gem|x"])
    store.add_history_snapshot(iid, now - 119 * day, ["gem|x"])
    # "Fresh": one play, yesterday -> recent, must be excluded
    store.add_history_snapshot(iid, now - 1 * day, ["fresh|x"])

    res = store.resurface_candidates(now=now, window_days=90, min_plays=2, limit=10)

    assert [r["title"] for r in res] == ["Gem"]
    assert res[0]["plays"] == 2
    assert res[0]["last_played"] == now - 119 * day


def test_for_you_builds_items_with_days_since(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Gem", "X", None, None)
    day = 86400.0
    now = 200 * day
    store.add_history_snapshot(iid, now - 120 * day, ["gem|x"])
    store.add_history_snapshot(iid, now - 100 * day, ["gem|x"])

    items = recommend.for_you(store, now=now, min_plays=2, limit=24)

    assert len(items) == 1
    assert items[0].title == "Gem"
    assert items[0].plays == 2
    assert items[0].days_since == 100   # newest play was 100 days ago


def test_take_action_surfaces_auth_sync_and_cleanup(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLEMPTY", "Empties", 0, "h", 0.0)   # empty playlist, no tracks

    items = recommend.take_action(store, now=1000.0, auth_expired={iid: "main"})

    kinds = [i.kind for i in items]
    assert kinds[0] == "auth"                       # auth first (highest severity)
    assert "Re-authenticate main" in items[0].title
    assert "sync" in kinds                           # no last_sync_at recorded -> "Time to sync"
    cleanup = [i for i in items if i.kind == "cleanup"]
    assert any("empty" in i.title.lower() and i.cta_href == "/cleanup" for i in cleanup)


def test_take_action_quiet_when_recently_synced_and_clean(store):
    store.upsert_identity("main", "cred", None, True)
    store.set_setting("last_sync_at", "1000.0")
    items = recommend.take_action(store, now=1000.0 + 60, auth_expired={})
    assert items == []
