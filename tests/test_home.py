from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1")


def test_home_visit_ticks_card_rotation_but_previews_dont(store):
    """A genuine Home visit advances each card's rotation counter once; steer/stance previews and the
    feed fragment re-render the same epoch without ticking — so tuning your taste never churns cards."""
    from yt_playlist.rec_dao import RecDao
    c = _client(store)
    dao = RecDao(store)
    assert dao.card_views("wheelhouse") == 0
    c.get("/")
    c.get("/")
    assert dao.card_views("wheelhouse") == 2 and dao.card_views("new_artists") == 2   # all cards tick
    c.post("/home/stance", data={"stance": "explore"})    # previews must not advance rotation
    c.post("/home/steer", data={"axis": "genre:Rock", "weight": "1.5"})
    c.get("/home/feed")
    c.get("/home/fresh")                                   # lazy re-fetch (e.g. from a steer) — read-only
    assert dao.card_views("wheelhouse") == 2 and dao.card_views("fresh") == 2


def test_home_is_default_route(store):
    c = _client(store)
    r = c.get("/")
    assert r.status_code == 200
    assert 'id="home"' in r.text          # Home shell marker
    assert "Never synced" in r.text        # sync card shows status (no last_sync_at yet)


def test_playlists_moved_to_slash_playlists(store):
    c = _client(store)
    assert c.get("/playlists").status_code == 200


def test_home_renders_for_you_and_no_sync_elsewhere(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Gem", "X", None, None)
    day = 86400.0
    now = 200 * day
    store.add_history_snapshot(iid, now - 120 * day, ["gem|x"])
    store.add_history_snapshot(iid, now - 110 * day, ["gem|x"])
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now)
    c = TestClient(app, base_url="http://127.0.0.1")

    home = c.get("/").text
    assert "More in your wheelhouse" in home    # the exploit lane heading
    assert "Gem" in home                       # the forgotten gem is rendered
    assert 'class="sync-bar"' in home          # Sync control present on Home

    # Sync control removed from the other tabs (Rediscover is deleted in Task 8)
    assert 'class="sync-bar"' not in c.get("/playlists").text
    assert 'class="sync-bar"' not in c.get("/charts").text


def test_home_feed_fragment_renders_fingerprint_and_respects_stance(store):
    c = _client(store)
    r = c.get("/home/feed")
    assert r.status_code == 200
    assert "fingerprint" in r.text                       # the header partial rendered
    assert 'id="home-feed"' in r.text                    # the re-rank swap-target container

    c.post("/home/stance", data={"stance": "explore"})
    assert store.get_setting("home_stance") == "explore"  # persisted


def test_home_page_has_steering_and_fingerprint(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(t, "Techno")
    store.set_track_year(t, "1999")
    store.add_history_snapshot(iid, 1.0, ["song|band"])
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    html = c.get("/").text
    assert 'id="fingerprint"' in html                    # fingerprint header present
    assert 'type="range"' in html                        # draggable bars, not +/- buttons
    assert "/home/steer" in html                         # dragging a bar steers + re-ranks
    assert "/home/stance" in html                        # explore/exploit toggle wired
    assert "/taste" in html                              # "Tune your taste model" affordance


def test_home_steer_sets_weight_and_returns_feed(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/home/steer", data={"axis": "genre:techno", "weight": "0.3"})
    assert r.status_code == 200
    assert 'id="home-feed"' in r.text                     # returns the re-ranked feed fragment
    assert store.get_weights()["genre:techno"] == 0.3     # weight set (genre band allows < 0.2)


def test_home_feed_has_steer_toast_scaffold(store):
    c = _client(store)
    html = c.get("/home/feed").text
    assert 'id="steer-toast"' in html
    assert "Tune your taste model" in html or "fine-tune" in html.lower()
