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
    from yt_playlist.rec.rec_dao import RecDao
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


def test_home_rediscovers_unplayed_saved_albums(store):
    # A saved album with no recent plays surfaces as a "Revisit" tile at the top of the Rediscover
    # section, linking to its in-app album page.
    store.set_setting("last_sync_at", "1.0")          # Rediscover only renders on a synced Home
    store.collection.add_saved_album({"browse": "MPREb_x", "title": "Kind of Blue", "artist": "Miles Davis",
                                      "year": "1959", "type": "Album", "thumbnail": "http://img/x.jpg"})
    c = _client(store)
    html = c.get("/").text
    assert "Rediscover in your library" in html
    assert "Kind of Blue" in html and "Miles Davis" in html
    assert "Revisit" in html                          # the relabeled badge (not "New album")
    assert "/album?browse=MPREb_x" in html            # tile links to the in-app album page


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
    store.set_setting("last_sync_at", str(now - day))   # synced user -> rec feed renders (not the placeholder)
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
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(t, "Techno")
    store.add_history_snapshot(iid, 1.0, ["song|band"])   # a played, genre-tagged track -> panel renders
    c = _client(store)
    r = c.get("/home/feed")
    assert r.status_code == 200
    assert "fingerprint" in r.text                       # the header partial rendered
    assert 'id="home-feed"' in r.text                    # the re-rank swap-target container

    c.post("/home/stance", data={"stance": "explore"})
    assert store.get_setting("home_stance") == "explore"


def test_steering_panel_hidden_until_genres_exist(store):
    """The wheelhouse steering panel (genre/era sliders) is empty-state until the library has genres."""
    iid = store.upsert_identity("main", "cred", None, True)
    c = _client(store)
    assert 'id="fingerprint"' not in c.get("/home/feed").text     # no genres yet -> panel hidden

    t = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(t, "Techno")
    store.add_history_snapshot(iid, 1.0, ["song|band"])
    assert 'id="fingerprint"' in c.get("/home/feed").text          # genres present -> panel appears  # persisted


def test_home_page_has_steering_and_fingerprint(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(t, "Techno")
    store.set_track_year(t, "1999")
    store.add_history_snapshot(iid, 1.0, ["song|band"])
    store.set_setting("last_sync_at", "1000")            # synced user -> feed (with fingerprint/steer) renders
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    html = c.get("/").text
    assert 'id="fingerprint"' in html                    # fingerprint header present
    assert 'type="range"' in html                        # draggable bars, not +/- buttons
    assert "/home/steer" in html                         # dragging a bar steers + re-ranks
    assert "/home/stance" in html                        # explore/exploit toggle wired
    assert "/taste" in html                              # "Tune your taste model" affordance


def test_home_steer_writes_lean_not_permanent_weight(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/home/steer", data={"axis": "genre:techno", "weight": "1.5"})
    assert r.status_code == 200
    assert 'id="home-feed"' in r.text
    assert "genre:techno" not in store.get_weights()      # NOT a permanent write anymore
    # permanent is neutral (1.0) so effective target 1.5 -> lean 1.5
    assert store.get_lean("genre:techno") == 1.5


def test_home_feed_has_steer_toast_scaffold(store):
    c = _client(store)
    html = c.get("/home/feed").text
    assert 'id="steer-toast"' in html
    assert "Tune your taste model" in html or "fine-tune" in html.lower()


def test_new_user_only_gets_full_sync(store):
    """A never-synced user gets a single green 'Sync now' CTA (no plays to sync yet). After the first
    sync it becomes the neutral 'Full sync' button and the 'Sync plays' auto-sync toggle appears."""
    c = _client(store)
    html = c.get("/").text
    assert "Never synced" in html
    assert "Sync now" in html                # initial CTA label (matches the setup flash)
    assert "sync-cta" in html                # ...and it's the green CTA
    assert "Full sync" not in html           # the neutral label is for after the first sync
    assert "Sync plays" not in html          # no plays to sync yet
    assert "sync-toggle" not in html

    store.set_setting("last_sync_at", "1000")   # now there's a first sync (now_fn -> 1000.0)
    html = c.get("/").text
    assert "Full sync" in html and "sync-cta" not in html   # reverts to the neutral ghost button
    assert "Sync plays" in html and "sync-toggle" in html


def test_presync_shows_recs_placeholder_not_feed(store):
    """Pre-sync, Home replaces the recommendation feed with a graphical placeholder; after the first
    sync the placeholder is gone and the feed renders."""
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Gem", "X", None, None)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")

    html = c.get("/").text
    assert 'class="presync card"' in html                                  # placeholder shown
    assert "we'll start getting you recommendations here" in html          # the copy
    assert "More in your wheelhouse" not in html                           # feed hidden pre-sync

    store.set_setting("last_sync_at", "1000")
    assert "presync card" not in c.get("/").text                           # placeholder gone post-sync


def test_enrich_nudge_after_first_sync_and_persisted_dismiss(store):
    """After the first sync, Home shows a one-time 'enrich improves recs' nudge. Dismissing it
    persists (settings flag) so it never returns. A never-synced user doesn't see it."""
    c = _client(store)
    assert "enrich-nudge" not in c.get("/").text          # never synced -> no nudge yet

    store.set_setting("last_sync_at", "1000")             # first sync happened
    html = c.get("/").text
    assert "enrich-nudge" in html
    assert "better your recommendations" in html          # the message landed

    r = c.post("/onboard/enrich/dismiss")                 # dismiss it
    assert r.status_code == 200
    assert store.get_setting("enrich_nudge_dismissed") == "1"

    assert "enrich-nudge" not in c.get("/").text          # gone for good


def test_auto_sync_toggle_persists_and_renders(store):
    store.set_setting("last_sync_at", "1000")   # synced user -> toggle is offered
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")

    assert "Will re-sync plays automatically every 30 mins" in c.get("/").text   # note copy present

    r = c.post("/sync/auto", data={"enabled": "1"})
    assert r.status_code == 200 and r.json() == {"enabled": True}
    assert store.get_setting("auto_sync_plays") == "1"
    assert "syncPanel(true)" in c.get("/").text                                   # reflected on load

    r = c.post("/sync/auto", data={"enabled": "0"})
    assert r.json() == {"enabled": False}
    assert store.get_setting("auto_sync_plays") == "0"
    assert "syncPanel(false)" in c.get("/").text


def test_fingerprint_bar_shows_effective_multiplier(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(t, "Techno")
    store.add_history_snapshot(iid, 1.0, ["song|band"])
    store.set_setting("last_sync_at", "1000")
    fam = __import__("yt_playlist.rec.genre_map", fromlist=["family"]).family("Techno")
    store.set_lean(f"genre:{fam}", 1.5, 1000.0)        # standing lean, permanent still 1.0
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    html = c.get("/").text
    assert 'value="1.5"' in html                       # bar shows effective, not permanent 1.0


def test_fingerprint_expand_lists_subgenres(store):
    """POST /home/fingerprint/expand family=techno -> contains a subgenre bar, e.g. 'genre:minimal techno'."""
    c = _client(store)
    r = c.post("/home/fingerprint/expand", data={"family": "techno"})
    assert r.status_code == 200
    # 'minimal techno' is a known subgenre of techno
    assert "minimal techno" in r.text
    # should render as fp-row slider posting to /home/steer
    assert "/home/steer" in r.text
    assert 'type="range"' in r.text


def test_fingerprint_add_reaches_zero_play_niche(store):
    """POST /home/fingerprint/add axis='genre:gqom' -> 200, 'genre:gqom' appears in returned HTML,
    and store.get_lean('genre:gqom') == 1.0."""
    c = _client(store)
    r = c.post("/home/fingerprint/add", data={"axis": "genre:gqom"})
    assert r.status_code == 200
    # The added axis must appear in the returned feed HTML (as a steerable bar)
    assert "gqom" in r.text
    # The lean was persisted
    assert store.get_lean("genre:gqom") == 1.0


def test_fingerprint_search_finds_genre(store):
    """GET /home/fingerprint/search?q=techno -> 200, contains 'minimal techno' (or another techno member)."""
    c = _client(store)
    r = c.get("/home/fingerprint/search?q=techno")
    assert r.status_code == 200
    # Should include at least one techno-related tag
    assert "techno" in r.text.lower()
    # Should include 'minimal techno' specifically (a known subgenre)
    assert "minimal techno" in r.text
