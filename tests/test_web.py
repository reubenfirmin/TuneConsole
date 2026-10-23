"""Application shell, local-host guards, navigation, search and toasts."""
from fastapi.testclient import TestClient
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.web_support import _client

def test_home_renders_toast_from_query_param(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    html = c.get("/?toast=You%E2%80%99re+authenticated+again.").text
    assert "toast-ok" in html and "authenticated again" in html
    assert "toast-ok" not in c.get("/").text          # absent without the param

def test_auth_expired_shows_reauth_banner(store):
    class Expired:   # a client whose session has lapsed
        def get_library_playlists(self, limit=None):
            raise RuntimeError("Server returned HTTP 401: Unauthorized")
    iid = store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: Expired()}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert app.state.ctx.auth_expired == {}          # nothing wrong before a sync
    assert "authBanner([])" in c.get("/").text       # banner seeded empty (hidden)
    jid = c.post("/sync").json()["job_id"]
    with c.stream("GET", f"/sync/events/{jid}") as s:
        "".join(s.iter_text())                        # drain to completion
    assert app.state.ctx.auth_expired == {iid: "main"}        # flagged for re-auth
    assert 'authBanner(["main"])' in c.get("/").text          # banner seeded with the label

def test_actions_route_renders(store):
    store.identities.upsert_identity("main", "cred", None, True)
    store.actions.record_action("merge", "{}", "[]", "executed", "{}", 1.0)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)
    r = c.get("/actions")
    assert r.status_code == 200
    assert "<title>Action Log · TuneConsole</title>" in r.text
    assert "<h1>Action Log</h1>" in r.text
    assert "Audit Log" not in r.text
    assert "merge" in r.text.lower()


def test_post_with_foreign_host_rejected(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://evil.example")   # DNS-rebound attacker host
    assert c.post("/sync").status_code == 400

def test_post_with_foreign_origin_rejected(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    # Host is local but the request was forged from another site -> blocked.
    r = c.post("/sync", headers={"origin": "https://evil.example"})
    assert r.status_code == 403

def test_post_with_local_origin_allowed(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fc = FakeClient(playlists=[], tracks={})
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/sync", headers={"origin": "http://127.0.0.1"}, follow_redirects=False)
    assert r.status_code == 200  # starts a sync job (JSON), no longer a redirect

def test_get_with_foreign_host_still_allowed(store):
    # Reads are exempt: the guard only protects state-changing methods.
    store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://evil.example")
    assert c.get("/").status_code == 200


def test_vendored_assets_served_locally(store):
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)
    for name in ("htmx.min.js", "alpine.min.js"):
        r = c.get(f"/static/vendor/{name}")
        assert r.status_code == 200 and len(r.content) > 1000

def test_no_external_cdn_in_pages(store):
    store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)
    body = c.get("/cleanup").text
    assert "unpkg.com" not in body and "cdn.jsdelivr" not in body
    assert "/static/vendor/htmx.min.js" in body


def test_tools_menu_only_shows_move_for_multiple_identities(store):
    store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)

    assert '<a href="/move"' not in c.get("/").text

    store.identities.upsert_identity("other", "cred-2", "brand-2", False)
    assert '<a href="/move"' in c.get("/").text


def test_action_log_is_last_in_tools_menu(store):
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    body = TestClient(app).get("/").text
    menu = body.split('class="tools-pop"', 1)[1].split("</div>", 1)[0]

    assert '>Action Log</a>' in menu
    assert menu.find('href="/setup"') < menu.find('href="/cleanup"')
    assert 'href="/genres"' not in menu
    assert menu.rfind('href="/enrich"') > menu.rfind('href="/setup"')
    assert menu.rfind('href="/network"') > menu.rfind('href="/enrich"')
    assert menu.rfind('href="/actions"') > menu.rfind('href="/network"')


def test_playlists_tab_renders(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.playlists.upsert_playlist(iid, "PLA", "Alpha", 3, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})
    page = c.get("/playlists").text                                # Playlists moved off / to /playlists
    assert "Playlists" in page and "playlistsTab(" in page
    assert c.get("/cleanup").status_code == 200                    # cleanup moved here
    assert 'href="/cleanup"' in page                              # nav points at it
    assert c.get("/").status_code == 200                          # / is now the Home tab


def test_liked_songs_get_a_heart(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    mix = store.playlists.upsert_playlist(iid, "PLM", "Mix", 2, "h", 1.0)
    lm = store.playlists.upsert_playlist(iid, "LM", "Liked Music", 1, "h", 1.0)   # the YouTube liked playlist
    loved = store.tracks.upsert_track("v1", "Loved", "A", "Al", 200, 1)
    store.playlists.set_playlist_tracks(mix, [loved, store.tracks.upsert_track("v2", "Plain", "B", "Al", 200, 1)])
    store.playlists.set_playlist_tracks(lm, [loved])

    liked = {t["video_id"]: t["liked"] for t in store.charts.playlist_tracks_detail(mix)}
    assert liked == {"v1": True, "v2": False}                       # song in LM is liked
    assert store.charts.artist_songs("A")[0]["liked"] is True             # and wherever the song appears
    c = _client(store, lambda: {iid: FakeClient()})
    assert "like-btn on" in c.get(f"/playlist/{mix}").text          # the liked song shows a filled heart


def test_remove_playlist_preserves_group(store):
    # Groups are user curation (not on YouTube). A removed/pruned playlist must keep its group so it
    # reattaches if the playlist comes back. Regression for the prune that wiped groups too.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLG", "Grouped", 1, "h", 1.0)
    store.playlists.set_playlist_group("PLG", "Workout")
    store.playlists.remove_playlist(a)
    assert store.playlists.get_playlist_groups() == {"PLG": "Workout"}      # survives removal
    store.playlists.upsert_playlist(iid, "PLG", "Grouped", 1, "h", 2.0)     # re-added on a later sync
    assert store.playlists.get_playlist_groups().get("PLG") == "Workout"    # grouping reattaches


def test_toasts_region_present_on_every_page(store):
    store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert 'id="toasts"' in c.get("/").text


def test_error_toast_partial_renders(store):
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    tmpl = app.state.ctx.templates.env.get_template("_partials/error_toast.html")
    html = tmpl.render(message="Boom")
    assert "Boom" in html
    assert 'hx-swap-oob="afterbegin:#toasts"' in html


def _seed_search(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    r1 = store.tracks.upsert_track("r1", "Spektrum", "Ritmo", "Ritmo LP", None,
                            album_browse_id="MPREb_ritmolp")
    pl = store.playlists.upsert_playlist(iid, "PL1", "Late Night Drive", 1, "h", 0.0)
    store.playlists.set_playlist_tracks(pl, [r1])
    return iid


def test_omni_search_returns_results_partial(store):
    _seed_search(store)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000.0)
    c = TestClient(app)
    r = c.get("/search/omni", params={"q": "ritmo"})
    assert r.status_code == 200
    assert "Tracks by Ritmo" in r.text
    assert f"/artist?artist_id={store.artists.resolve('Ritmo')}" in r.text


def test_omni_search_short_query_renders_nothing(store):
    _seed_search(store)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000.0)
    c = TestClient(app)
    r = c.get("/search/omni", params={"q": "r"})
    assert r.status_code == 200
    assert "omni-row" not in r.text       # no result rows for a 1-char query


def test_navbar_has_omni_search(store):
    app = create_app(store, lambda: {}, now_fn=lambda: 1000.0)
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert 'class="omni"' in r.text
    assert 'hx-get="/search/omni"' in r.text
