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


def test_for_you_blends_real_signals_with_reasons(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # Artist you play a lot: "Hit" played, "Neglected" not -> a deep cut
    hit = store.upsert_track("v1", "Hit", "Fav", None, None)        # key "hit|fav"
    store.upsert_track("v2", "Neglected", "Fav", None, None)        # key "neglected|fav"
    # A neighbour that co-occurs in a playlist with your most-played track
    nb = store.upsert_track("v3", "Neighbour", "Other", None, None)  # key "neighbour|other"
    pl = store.upsert_playlist(iid, "PL1", "Mix", 2, "h", 0.0)
    store.set_playlist_tracks(pl, [hit, nb])
    now = 1000.0
    store.add_history_snapshot(iid, now - 100, ["hit|fav"])
    store.add_history_snapshot(iid, now - 50, ["hit|fav"])

    items = recommend.for_you(store, now=now, limit=10)
    titles = {i.title for i in items}

    assert "Neglected" in titles          # deep cut from an artist you play
    assert "Neighbour" in titles          # shares a playlist with your most-played
    assert "Hit" not in titles            # your already-played seed isn't recommended back
    assert all(i.reason for i in items)   # every rec explains itself


def test_for_you_never_empty_without_history_depth(store):
    """The day-one failure that shipped empty: a normal library must still yield recs."""
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "Played", "Band", None, None)     # key "played|band"
    store.upsert_track("v2", "Bench", "Band", None, None)          # neglected by same artist
    store.set_playlist_tracks(store.upsert_playlist(iid, "PL", "P", 1, "h", 0.0), [a])
    now = 1000.0
    store.add_history_snapshot(iid, now - 60, ["played|band"])     # all history is RECENT
    store.add_history_snapshot(iid, now - 30, ["played|band"])

    items = recommend.for_you(store, now=now, limit=10)
    assert items, "for_you must not be empty on a fresh library with plays"


def test_complete_playlist_suggests_fitting_owned_tracks(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "Anchor", "Band", None, None)      # in the target playlist
    b = store.upsert_track("v2", "Bonus", "Band", None, None)       # same artist, not in target
    c = store.upsert_track("v3", "Cooc", "Other", None, None)       # co-occurs with anchor elsewhere
    target = store.upsert_playlist(iid, "PT", "Target", 1, "h", 0.0)
    store.set_playlist_tracks(target, [a])
    other = store.upsert_playlist(iid, "PO", "Other", 3, "h2", 0.0)
    store.set_playlist_tracks(other, [a, b, c])                     # anchor co-occurs with b and c here

    items = recommend.complete_playlist(store, target, limit=10)
    titles = {i.title for i in items}

    assert "Bonus" in titles          # same artist as a member
    assert "Cooc" in titles           # co-occurs with a member in another playlist
    assert "Anchor" not in titles     # already in the playlist
    assert all(i.reason for i in items)


def test_take_action_auth_and_cleanup_no_sync(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLEMPTY", "Empties", 0, "h", 0.0)   # empty playlist

    items = recommend.take_action(store, now=1000.0, auth_expired={iid: "main"})

    kinds = [i.kind for i in items]
    assert "sync" not in kinds                       # sync nudge now lives in the Sync card
    assert kinds[0] == "auth"
    assert "Re-authenticate main" in items[0].title
    assert any(i.kind == "cleanup" and i.cta_href == "/cleanup" for i in items)


def test_take_action_empty_when_clean(store):
    store.upsert_identity("main", "cred", None, True)
    assert recommend.take_action(store, now=1000.0, auth_expired={}) == []


def test_take_action_enrichment_ranked_by_playcount(store):
    iid = store.upsert_identity("main", "cred", None, True)
    hot = store.upsert_track("v1", "Hot", "A", None, None)    # no genre -> gap
    cold = store.upsert_track("v2", "Cold", "B", None, None)  # no genre -> gap
    php = store.upsert_playlist(iid, "PH", "Hot List", 1, "h", 0.0, "http://t/hot.jpg")
    plp = store.upsert_playlist(iid, "PL", "Cold List", 1, "h2", 0.0)
    store.set_playlist_tracks(php, [hot])
    store.set_playlist_tracks(plp, [cold])
    store.add_history_snapshot(iid, 1.0, ["hot|a"])           # only Hot List has plays

    items = recommend.take_action(store, now=1000.0, auth_expired={})
    enrich = [i for i in items if i.kind == "enrich"]

    assert len(enrich) == 2                           # one card per gappy playlist
    assert all(i.severity == "low" for i in enrich)
    assert "Hot List" in enrich[0].title              # ranked by playcount: Hot List first
    assert "Cold List" in enrich[1].title
    assert enrich[0].cta_href == f"/playlist/{php}"
    assert enrich[0].thumbnail == "http://t/hot.jpg"  # card carries the playlist thumbnail


def test_sync_status_never_synced(store):
    st = recommend.sync_status(store, now=1000.0)
    assert st.last_synced_ago is None and st.stale and st.message


def test_sync_status_recent_is_not_stale(store):
    store.set_setting("last_sync_at", "1000.0")
    st = recommend.sync_status(store, now=1000.0 + 60)
    assert st.stale is False and st.message is None and st.last_synced_ago


def test_sync_status_over_24h_is_stale(store):
    store.set_setting("last_sync_at", "1000.0")
    st = recommend.sync_status(store, now=1000.0 + 25 * 3600)
    assert st.stale is True and st.message
