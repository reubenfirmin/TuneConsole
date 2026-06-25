"""Generated proto-playlists: the two dated, saveable lanes on Home.

Covers the load-bearing constraint — a playlist this app generates (auto-grouped "Generated") must
NOT feed the recommendation engine until it's played or re-grouped — plus the create endpoint.
"""
import json

from fastapi.testclient import TestClient

from yt_playlist.util.matching import identity_key
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def _seed_generated(store, iid, n=3):
    """A generated-group playlist of n tracks (unplayed). Returns (pid, ytm, track identity_keys)."""
    pid = store.upsert_playlist(iid, "PLG", "Gen - June 21 2026", n, "h", 1.0)
    tids = [store.upsert_track(f"g{i}", f"G{i}", "GenArt", None, None, 1) for i in range(n)]
    store.set_playlist_tracks(pid, tids)
    store.set_playlist_group("PLG", "Generated")
    return pid, "PLG", {identity_key(f"G{i}", "GenArt") for i in range(n)}


def test_generated_excluded_until_promoted(store):
    iid = store.upsert_identity("main", "cred", None, True)
    pid, ytm, gkeys = _seed_generated(store, iid, n=2)
    dao = RecDao(store)

    assert dao.excluded_playlist_ids() == {pid}                 # quarantined while "Generated"

    # Playing it does NOT graduate it — adoption is an explicit act, not a side effect of listening.
    for _ in range(5):
        store.add_history_snapshot(iid, 1.0, list(gkeys))
    assert dao.excluded_playlist_ids() == {pid}                 # still quarantined despite heavy plays

    store.set_playlist_group(ytm, "Faves")                      # promotion out of the group = adoption
    assert dao.excluded_playlist_ids() == set()

    store.set_playlist_group(ytm, "Generated")                  # back into quarantine
    assert dao.excluded_playlist_ids() == {pid}


def test_generated_tracks_in_no_basket_until_promoted(store):
    iid = store.upsert_identity("main", "cred", None, True)
    _pid, ytm, gkeys = _seed_generated(store, iid, n=3)
    dao = RecDao(store)

    for _ in range(2):
        store.add_history_snapshot(iid, 1.0, list(gkeys))      # even played, while still "Generated"...
    keys = {k for b in dao.rec_baskets() for k in b}
    assert not (keys & gkeys)                                   # ...generated songs pollute no basket

    store.set_playlist_group(ytm, "Faves")                     # promote it into the collection
    keys2 = {k for b in dao.rec_baskets() for k in b}
    assert gkeys & keys2                                        # ...so they rejoin the signal


def test_generate_endpoint_creates_and_groups(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient()
    c = _client(store, lambda: {iid: fc})
    tracks = json.dumps([{"video_id": "v1", "title": "S1", "artist": "A", "album": "", "thumbnail": ""},
                         {"video_id": "v2", "title": "S2", "artist": "A", "album": "", "thumbnail": ""}])

    r = c.post("/home/generate", data={"name": "More in your wheelhouse - June 21 2026", "tracks": tracks})

    assert r.status_code == 200 and "Saved" in r.text
    assert fc.created and fc.created[0][1] == "More in your wheelhouse - June 21 2026"
    assert fc.added and fc.added[0][1] == ["v1", "v2"]
    new_ytm = fc.created[0][0]
    assert store.get_playlist_groups().get(new_ytm) == "Generated"   # auto-grouped
    # optimistically materialized so it shows in the Playlists tab right away (no sync needed)
    saved = next(p for p in store.get_playlists() if p.ytm_playlist_id == new_ytm)
    assert saved.title == "More in your wheelhouse - June 21 2026" and saved.track_count == 2


def test_generate_result_reuses_preopened_tab(store):
    """Regression: the post-save swap must point an ALREADY-OPEN tab at the playlist, not call a bare
    window.open() itself. A window.open() fired from the htmx response runs after the save round-trip,
    which — once the batch-add falls back to slow per-item retries — outlives the browser's user-
    activation window and gets popup-blocked (the YouTube tab never opens, only the same-tab redirect
    survives). So the success path reuses the tab opened synchronously during the click."""
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient()
    c = _client(store, lambda: {iid: fc})
    tracks = json.dumps([{"video_id": "v1", "title": "S1", "artist": "A", "album": "", "thumbnail": ""}])

    r = c.post("/home/generate", data={"name": "Mix - June 21 2026", "tracks": tracks})

    assert r.status_code == 200 and "Saved" in r.text
    assert "__ytTab" in r.text                                  # reuses the tab opened on click


def test_save_button_preopens_youtube_tab(store):
    """The Save & play button must open the blank YouTube tab DURING the click (a user gesture), so the
    browser doesn't block it. Without this, the open is deferred to the slow post-save swap and blocked."""
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Artist", "Alb", None, 1)
    pl = store.upsert_playlist(iid, "PL", "P", 1, "h", 1.0)
    store.set_playlist_tracks(pl, [t])
    store.add_history_snapshot(iid, 1.0, [identity_key("Song", "Artist")])
    store.set_setting("last_sync_at", "1.0")        # synced -> the rec feed (generated cards) renders
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.get("/")

    assert r.status_code == 200
    # the Save & play button opens the tab on click and stashes the handle for the swap to reuse
    assert "genOpenYT(" in r.text and "__ytTab" in r.text


def test_saved_proto_tracks_not_re_offered(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = [store.upsert_track(f"v{i}", f"S{i}", "Art", None, None, 1) for i in range(2)]
    gen = store.upsert_playlist(iid, "PLG", "From your catalog - June 21 2026", 2, "h", 1.0)
    store.set_playlist_tracks(gen, t)
    store.set_playlist_group("PLG", "Generated")

    keys = RecDao(store).generated_track_keys()
    assert keys == {identity_key("S0", "Art"), identity_key("S1", "Art")}   # spoken for; don't re-offer


def test_generate_endpoint_rejects_empty(store):
    iid = store.upsert_identity("main", "cred", None, True)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post("/home/generate", data={"name": "x", "tracks": "[]"})
    assert r.status_code == 200 and "Couldn't save" in r.text


def test_home_renders_generated_cards(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Artist", "Alb", None, 1)
    pl = store.upsert_playlist(iid, "PL", "P", 1, "h", 1.0)
    store.set_playlist_tracks(pl, [t])
    store.add_history_snapshot(iid, 1.0, [identity_key("Song", "Artist")])
    store.set_setting("last_sync_at", "1.0")        # synced -> the rec feed (generated cards) renders
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.get("/")
    assert r.status_code == 200
    assert "More in your wheelhouse" in r.text and "Save &amp; play on YouTube" in r.text


def test_comfort_proto_card_dj_ordered_with_genre(store, monkeypatch):
    """A proto-card must be ordered BEFORE it's shown: no back-to-back same artist, and genres
    attached so a genre journey is possible. Reproduces the 'comfort playlist clustered by artist,
    no genre journey' bug — the DJ used to run only at save, on data that had already dropped genre.
    Uses shuffle journey so the within-band artist-spacing guarantee applies to all items together."""
    from yt_playlist.web.routes import home
    from yt_playlist.rec import recommend
    from yt_playlist.rec.recommend import ForYouItem
    store.upsert_identity("main", "cred", None, True)
    spec = ([("Hermanos", "Latin")] * 4 + [("Younger", "Electronica")] * 3
            + [("Ritmo", "Electronica")] * 2 + [("Supertramp", "Rock")])   # arrives artist-clustered
    items = []
    for i, (art, genre) in enumerate(spec):
        tid = store.upsert_track(f"v{i}", f"S{i}", art, "", None, 1)
        store.set_track_genre(tid, genre)
        items.append(ForYouItem(f"S{i}", art, "", f"v{i}", None, 5, "most-played",
                                identity_key(f"S{i}", art), "comfort"))
    monkeypatch.setattr(recommend, "roll_recipe",
                        lambda *a, **k: {"facets": {}, "journey": "shuffle", "dj": {"seed": 1}})

    card = home._carded(store, "comfort", "Comfort listening", items, now=1.0)

    out = card["tracks"]
    assert len(out) == len(items)                                   # all rows survive the ordering
    adj_same = sum(1 for a, b in zip(out, out[1:]) if a.artist == b.artist)
    assert adj_same == 0                                            # artists spaced in the PREVIEW
    assert all(getattr(t, "genre", "") for t in out)               # genres attached -> journey possible


def test_create_generated_playlist_stores_recipe_and_versions(store):
    from yt_playlist.library import executor
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient()
    tracks = [{"video_id": f"v{i}", "title": f"S{i}", "artist": "A" if i % 2 else "B"} for i in range(4)]
    recipe = {"model": "fresh", "facets": {"genres": ["house"], "eras": ["2010"]},
              "dj": {"stickiness": 0.0, "seed": 5}}
    r1 = executor.create_generated_playlist(store, "Fresh songs - June 21 2026", list(tracks),
                                            fc, now=1.0, identity_id=iid, recipe=recipe)
    assert r1["title"] == "Fresh songs - June 21 2026 #1"          # versioned at save
    assert store.get_recipe(r1["new_ytm"])["facets"]["genres"] == ["house"]   # recipe stored
    r2 = executor.create_generated_playlist(store, "Fresh songs - June 21 2026", list(tracks),
                                            fc, now=2.0, identity_id=iid, recipe=recipe)
    assert r2["title"] == "Fresh songs - June 21 2026 #2"          # next version that day


# --- garbage collection of unplayed generated playlists (daily worker) -------------------------

def _seed_generated_at(store, iid, created, n=3, ytm="PLG"):
    """A Generated-group playlist of n unplayed tracks whose first_seen (creation) is `created`."""
    pid = store.upsert_playlist(iid, ytm, "Gen mix", n, "h", created)
    tids = [store.upsert_track(f"{ytm}{i}", f"{ytm}T{i}", "GenArt", None, None, 1) for i in range(n)]
    store.set_playlist_tracks(pid, tids)
    store.set_playlist_group(ytm, "Generated")
    return pid, ytm, {identity_key(f"{ytm}T{i}", "GenArt") for i in range(n)}


def test_gc_deletes_stale_unplayed_generated(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor
    from yt_playlist.util.action_kinds import is_undoable
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    pid, ytm, _ = _seed_generated_at(store, iid, created=now - 10 * day)   # 10d old, never played
    fc = FakeClient()

    collected = executor.gc_generated_playlists(store, {iid: fc}, now)     # 7-day grace by default

    assert [c["ytm"] for c in collected] == [ytm]
    assert store.get_playlist(pid) is None         # pruned locally
    assert fc.deleted == [ytm]                      # deleted on YouTube
    act = store.get_actions()[0]
    assert act.kind == "gc_generated" and is_undoable(act.kind)


def test_gc_keeps_young_generated(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    pid, _ytm, _ = _seed_generated_at(store, iid, created=now - 2 * day)   # still inside the grace window
    fc = FakeClient()

    assert executor.gc_generated_playlists(store, {iid: fc}, now) == []
    assert store.get_playlist(pid) is not None and fc.deleted == []


def test_gc_keeps_generated_played_since_creation(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    pid, _ytm, keys = _seed_generated_at(store, iid, created=now - 10 * day)
    store.add_history_snapshot(iid, now - 1 * day, list(keys))             # full playthrough since creation

    assert executor.gc_generated_playlists(store, {iid: FakeClient()}, now) == []
    assert store.get_playlist(pid) is not None


def test_gc_keeps_generated_when_at_least_half_played(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    pid, _ytm, _ = _seed_generated_at(store, iid, created=now - 10 * day, n=4)
    half = [identity_key(f"PLGT{i}", "GenArt") for i in (0, 1)]            # 2 of 4 = 50% played since creation
    store.add_history_snapshot(iid, now - 1 * day, half)

    assert executor.gc_generated_playlists(store, {iid: FakeClient()}, now) == []
    assert store.get_playlist(pid) is not None


def test_gc_deletes_generated_when_under_half_played(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    _pid, ytm, _ = _seed_generated_at(store, iid, created=now - 10 * day, n=4)
    store.add_history_snapshot(iid, now - 1 * day, [identity_key("PLGT0", "GenArt")])   # only 1 of 4 = 25%
    fc = FakeClient()

    collected = executor.gc_generated_playlists(store, {iid: fc}, now)
    assert [c["ytm"] for c in collected] == [ytm]                          # a stray play or two doesn't save it


def test_gc_ignores_plays_before_creation(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    _pid, ytm, keys = _seed_generated_at(store, iid, created=now - 10 * day)
    store.add_history_snapshot(iid, now - 20 * day, list(keys))            # only ever played pre-creation
    fc = FakeClient()

    collected = executor.gc_generated_playlists(store, {iid: fc}, now)
    assert [c["ytm"] for c in collected] == [ytm]                          # a pre-creation play doesn't save it


def test_gc_ignores_non_generated_playlists(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    t = store.upsert_track("v1", "S", "A", None, None, 1)
    pl = store.upsert_playlist(iid, "PLN", "Normal", 1, "h", now - 30 * day)   # old + unplayed, but real
    store.set_playlist_tracks(pl, [t])
    fc = FakeClient()

    assert executor.gc_generated_playlists(store, {iid: fc}, now) == []
    assert store.get_playlist(pl) is not None and fc.deleted == []


def test_gc_deletion_is_undoable(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library import executor
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0; now = 100 * day
    _pid, ytm, _ = _seed_generated_at(store, iid, created=now - 10 * day)
    fc = FakeClient()
    executor.gc_generated_playlists(store, {iid: fc}, now)
    aid = store.get_actions()[0].id

    executor.undo_action(store, aid, {iid: fc}, now + day)
    assert fc.created and fc.created[0][1] == "Gen mix"     # recreated from backup
    assert store.get_action(aid).status == "undone"


def test_carded_orders_by_recipe_journey(store, monkeypatch):
    """_carded must order the proto by the recipe's journey. Force 'warm_up' and assert the preview
    rises in energy thirds (and still spaces artists / carries genre)."""
    from yt_playlist.web.routes import home
    from yt_playlist.rec import recommend, journeys, genre_map
    from yt_playlist.rec.recommend import ForYouItem
    store.upsert_identity("main", "cred", None, True)
    # mellow vs intense families, interleaved so source order is NOT already sorted.
    spec = [("ambient", "Calm"), ("metal", "Loud"), ("classical", "Calm"), ("punk", "Loud"),
            ("folk", "Calm"), ("dnb", "Loud"), ("jazz", "Calm"), ("trance", "Loud"),
            ("blues", "Calm"), ("techno", "Loud"), ("ambient", "Calm"), ("metal", "Loud")]
    items = []
    for i, (genre, artist) in enumerate(spec):
        tid = store.upsert_track(f"v{i}", f"S{i}", artist, "", None, 1)
        store.set_track_genre(tid, genre)
        items.append(ForYouItem(f"S{i}", artist, "", f"v{i}", None, 1, "r",
                                identity_key(f"S{i}", artist), "comfort"))
    monkeypatch.setattr(recommend, "roll_recipe",
                        lambda *a, **k: {"facets": {}, "journey": "warm_up", "dj": {"seed": 1}})

    card = home._carded(store, "comfort", "Comfort listening", list(items), now=1.0)

    out = card["tracks"]
    assert card["recipe"]["journey"] == "warm_up"
    e = [genre_map.energy(t.genre) for t in out]
    thirds = [sum(e[:4]) / 4, sum(e[4:8]) / 4, sum(e[8:]) / 4]
    assert thirds[0] < thirds[1] < thirds[2]               # warm_up rises across ALL thirds (shuffle ~17%)
    assert all(getattr(t, "genre", "") for t in out)        # genres still attached
