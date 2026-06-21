from fastapi.testclient import TestClient
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track

def test_dashboard_and_sync(store):
    iid = store.upsert_identity("main", "cred", None, True)
    client = FakeClient(
        playlists=[{"playlistId": "PL1", "title": "Mix", "count": 1}],
        tracks={"PL1": [_track("v1", "A", "X")]})
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")

    assert c.get("/").status_code == 200
    # /sync now starts a background job and streams progress over SSE
    r = c.post("/sync")
    assert r.status_code == 200
    jid = r.json()["job_id"]
    with c.stream("GET", f"/sync/events/{jid}") as s:   # reading to EOF waits for the job to finish
        body = "".join(s.iter_text())
    assert '"type": "end"' in body
    assert len(store.get_playlists()) == 1

def test_auth_expired_shows_reauth_banner(store):
    class Expired:   # a client whose session has lapsed
        def get_library_playlists(self, limit=None):
            raise RuntimeError("Server returned HTTP 401: Unauthorized")
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: Expired()}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert app.state.ctx.auth_expired == {}          # nothing wrong before a sync
    assert "authBanner([])" in c.get("/").text       # banner seeded empty (hidden)
    jid = c.post("/sync").json()["job_id"]
    with c.stream("GET", f"/sync/events/{jid}") as s:
        "".join(s.iter_text())                        # drain to completion
    assert app.state.ctx.auth_expired == {iid: "main"}        # flagged for re-auth
    assert 'authBanner(["main"])' in c.get("/").text          # banner seeded with the label

def test_dupe_detail_renders(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "A", 1, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "B", 1, "h", 1.0)
    t = store.upsert_track("v1", "Shared", "X", None, None)
    store.set_playlist_tracks(a, [t]); store.set_playlist_tracks(b, [t])
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)
    r = c.get(f"/dupe/{a}/{b}")
    assert r.status_code == 200
    assert "shared|x" in r.text.lower() or "shared" in r.text.lower()

def test_dupe_detail_unknown_id_404(store):
    store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)
    assert c.get("/dupe/999/998").status_code == 404

def test_actions_route_renders(store):
    store.upsert_identity("main", "cred", None, True)
    store.record_action("merge", "{}", "[]", "executed", "{}", 1.0)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)
    r = c.get("/actions")
    assert r.status_code == 200
    assert "merge" in r.text.lower()


def _client(store, provider):
    from fastapi.testclient import TestClient
    # base_url is a local host so POSTs pass the cross-origin guard.
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")

def test_post_with_foreign_host_rejected(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://evil.example")   # DNS-rebound attacker host
    assert c.post("/sync").status_code == 400

def test_post_with_foreign_origin_rejected(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    # Host is local but the request was forged from another site -> blocked.
    r = c.post("/sync", headers={"origin": "https://evil.example"})
    assert r.status_code == 403

def test_post_with_local_origin_allowed(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient(playlists=[], tracks={})
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/sync", headers={"origin": "http://127.0.0.1"}, follow_redirects=False)
    assert r.status_code == 200  # starts a sync job (JSON), no longer a redirect

def test_rediscover_dismiss_fades_and_drops_candidate(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLZ", "Old Mix", 3, "h", 0.0)
    c = _client(store, lambda: {iid: FakeClient()})

    assert c.get("/discover").text.count('class="stale-row"') == 1
    r = c.post("/rediscover/dismiss", data={"ytm": "PLZ"})
    assert r.status_code == 200

    body = c.get("/discover").text
    assert body.count('class="stale-row"') == 0       # no longer a candidate
    assert "dismissed" in body and "PLZ" in body       # shown in snoozed section on reload

    r = c.post("/rediscover/restore", data={"ytm": "PLZ"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/discover").text.count('class="stale-row"') == 1

def test_rediscover_dismiss_without_ytm_returns_toast(store):
    store.upsert_identity("main", "cred", None, True)
    c = _client(store, lambda: {})
    r = c.post("/rediscover/dismiss", data={})
    assert r.status_code == 422
    assert r.headers.get("hx-reswap") == "none"
    assert "Nothing to dismiss" in r.text
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text

def test_rediscover_snooze_fades_and_hides(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLZ", "Old Mix", 3, "h", 0.0)
    c = _client(store, lambda: {iid: FakeClient()})
    assert c.post("/rediscover/snooze", data={"ytm": "PLZ", "days": "30"}).status_code == 200
    assert c.get("/discover").text.count('class="stale-row"') == 0
    assert store.get_stale_hidden_ytm(now=1.0) == {"PLZ"}
    assert store.get_stale_hidden_ytm(now=1.0 + 31 * 86400) == set()

def test_get_with_foreign_host_still_allowed(store):
    # Reads are exempt: the guard only protects state-changing methods.
    store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://evil.example")
    assert c.get("/").status_code == 200


# --- setup wizard (guided config creation) ---

class _FakeRuntime:
    """Minimal setup collaborator for create_app(setup=...)."""
    def __init__(self, store, configured=False, credentials_present=False):
        self.store = store
        self._configured = configured
        self.credentials_present = credentials_present
        self.applied = None
        self.raise_value_error = None
        self.account_name = "Tester"
        self.check_error = None
    @property
    def configured(self):
        return self._configured
    def clients(self):
        return {}
    def check_auth(self, capture):
        if self.check_error:
            raise ValueError(self.check_error)
        return self.account_name
    def apply_setup(self, headers_raw, identities):
        if self.raise_value_error:
            raise ValueError(self.raise_value_error)
        self.applied = (headers_raw, identities)
        self._configured = True

def test_unconfigured_redirects_to_setup(store):
    rt = _FakeRuntime(store, configured=False)
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert c.get("/setup").status_code == 200            # setup page itself is reachable

def test_setup_post_applies_and_redirects(store):
    rt = _FakeRuntime(store, configured=False)
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/setup", data={
        "headers": "Cookie: SID=abc",
        "label": ["main", "brand"],
        "brand_account_id": ["", "UC9"],
        "master": "0",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/")  # dashboard, with a flash
    headers_raw, identities = rt.applied
    assert headers_raw == "Cookie: SID=abc"
    assert [i["label"] for i in identities] == ["main", "brand"]
    assert identities[0]["is_master"] and not identities[1]["is_master"]
    assert identities[1]["brand_account_id"] == "UC9"

def test_setup_post_skips_blank_label_rows(store):
    rt = _FakeRuntime(store, configured=False)
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.post("/setup", data={
        "headers": "h",
        "label": ["main", ""],            # second row blank -> dropped
        "brand_account_id": ["", ""],
        "master": "0",
    }, follow_redirects=False)
    _, identities = rt.applied
    assert [i["label"] for i in identities] == ["main"]

def test_setup_post_validation_error_rerenders_400(store):
    rt = _FakeRuntime(store, configured=False)
    rt.raise_value_error = "exactly one identity must be the master, found 0"
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/setup", data={"headers": "h", "label": "main", "brand_account_id": ""})
    assert r.status_code == 400
    assert "must be the master" in r.text

def test_setup_post_without_collaborator_404(store):
    # No setup collaborator -> wizard POST is inert (existing apps keep working).
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.post("/setup", data={"label": "x"}).status_code == 404


# --- vendored front-end assets (no third-party CDN at runtime) ---

def test_vendored_assets_served_locally(store):
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)
    for name in ("htmx.min.js", "alpine.min.js"):
        r = c.get(f"/static/vendor/{name}")
        assert r.status_code == 200 and len(r.content) > 1000

def test_no_external_cdn_in_pages(store):
    store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)
    body = c.get("/cleanup").text
    assert "unpkg.com" not in body and "cdn.jsdelivr" not in body
    assert "/static/vendor/htmx.min.js" in body


def test_setup_check_reports_account(store):
    rt = _FakeRuntime(store); rt.account_name = "Reuben"
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/setup/check", data={"headers": "cookie: x"})
    assert r.status_code == 200
    assert "Signed in as" in r.text and "Reuben" in r.text       # result fragment
    assert "Reuben" in r.headers.get("hx-trigger", "")           # HX-Trigger carries the account

def test_setup_check_reports_error(store):
    rt = _FakeRuntime(store); rt.check_error = "sign-in didn't work (401)"
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/setup/check", data={"headers": "bad"})
    assert r.status_code == 200
    assert "sign-in" in r.text                                    # error rendered in the fragment
    assert '"ok": false' in r.headers.get("hx-trigger", "").lower()


def test_setup_page_alpine_attr_well_formed(store):
    # Regression: tojson emits double quotes, so x-data MUST be single-quoted or the JSON
    # terminates the HTML attribute early and Alpine (the whole check UI) never initializes.
    rt = _FakeRuntime(store, configured=False)
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    body = c.get("/setup").text
    assert "x-data='setupForm(" in body          # single-quoted wrapper
    assert 'x-data="setupForm([{"' not in body   # not the broken double-quoted form


# --- streaming sync (background job + SSE) ---

def test_sync_streams_progress_events(store):
    iid = store.upsert_identity("main", "cred", None, True)
    client = FakeClient(
        playlists=[{"playlistId": "PL1", "title": "Roadtrip", "count": 1}],
        tracks={"PL1": [_track("v1", "Song", "Artist")]})
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    jid = c.post("/sync").json()["job_id"]
    with c.stream("GET", f"/sync/events/{jid}") as s:
        body = "".join(s.iter_text())
    assert "Roadtrip" in body            # per-playlist progress line streamed
    assert "sync complete" in body       # final done event
    assert '"type": "end"' in body       # stream terminator
    assert len(store.get_playlists()) == 1

def test_sync_events_unknown_job_404(store):
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/sync/events/99999").status_code == 404

def test_sync_reports_failure_in_stream(store):
    iid = store.upsert_identity("main", "cred", None, True)
    class Boom:
        def get_library_playlists(self, limit=None): raise RuntimeError("api down")
    app = create_app(store, lambda: {iid: Boom()}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    jid = c.post("/sync").json()["job_id"]
    with c.stream("GET", f"/sync/events/{jid}") as s:
        body = "".join(s.iter_text())
    assert "sync failed" in body and "api down" in body


def test_identical_pair_opens_editor(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Mix A", 1, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "Mix B", 1, "h", 1.0)
    t = store.upsert_track("v1", "Song", "X", None, None)
    store.set_playlist_tracks(a, [t]); store.set_playlist_tracks(b, [t])
    c = _client(store, lambda: {iid: FakeClient()})
    body = c.get(f"/dupe/{a}/{b}").text   # redirects into the N-way editor
    assert "mergeEditor(" in body and "Keep" in body


def test_overlap_suppress_and_unsuppress(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fav = store.upsert_playlist(iid, "PLFAV", "Favorite Songs", 2, "h", 1.0)
    sml = store.upsert_playlist(iid, "PLSML", "Little Mix", 1, "h", 1.0)
    t1 = store.upsert_track("v1", "A", "X", None, 1); t2 = store.upsert_track("v2", "B", "X", None, 1)
    store.set_playlist_tracks(fav, [t1, t2]); store.set_playlist_tracks(sml, [t1])
    c = _client(store, lambda: {iid: FakeClient()})
    assert "Little Mix" in c.get("/cleanup").text
    r = c.post("/overlaps/suppress", data={"a": "PLFAV", "b": "PLSML"})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"   # pair -> Hidden, recompute
    # check the section header specifically (other copy may mention "hidden")
    assert 'Hidden overlaps <span class="count">' in c.get("/cleanup").text
    c.post("/overlaps/unsuppress", data={"a": "PLSML", "b": "PLFAV"})
    assert 'Hidden overlaps <span class="count">' not in c.get("/cleanup").text


def test_overlaps_suppress_many(store):
    import json
    iid = store.upsert_identity("main", "cred", None, True)
    fav = store.upsert_playlist(iid, "PLFAV", "Favorite Songs", 3, "h", 1.0)
    p1 = store.upsert_playlist(iid, "PL1", "One", 2, "h", 1.0)
    p2 = store.upsert_playlist(iid, "PL2", "Two", 1, "h", 1.0)
    t = [store.upsert_track(f"v{i}", f"S{i}", "X", None, None, 1) for i in range(3)]
    store.set_playlist_tracks(fav, t)
    store.set_playlist_tracks(p1, [t[0], t[1]])
    store.set_playlist_tracks(p2, [t[0]])
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post("/overlaps/suppress-many",
               data={"pairs": json.dumps([["PLFAV", "PL2"], ["PLFAV", "PL1"]])})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    assert "Hidden overlaps" in c.get("/cleanup").text
    # malformed entries are ignored, not fatal
    r = c.post("/overlaps/suppress-many", data={"pairs": json.dumps([["only-one"], "junk", []])})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"


def test_inline_dupe_delete_ok(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "X", None, 1)
    keep = store.upsert_playlist(iid, "KEEP", "Storm", 1, "h", 1.0); store.set_playlist_tracks(keep, [t])
    dele = store.upsert_playlist(iid, "DEL", "Storm", 1, "h", 1.0); store.set_playlist_tracks(dele, [t])
    client = FakeClient(tracks={"KEEP": [_track("v1", "Song", "X")], "DEL": [_track("v1", "Song", "X")]})
    c = _client(store, lambda: {iid: client})
    r = c.post("/dupe/delete", data={"source": dele, "target": keep})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.deleted == ["DEL"] and store.get_playlist(dele) is None

def test_inline_dupe_delete_refuses_survivor(store, monkeypatch, tmp_path):
    # Foot-gun guard: after one copy is gone, deleting the other must be refused (kept copy missing).
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "X", None, 1)
    survivor = store.upsert_playlist(iid, "SURV", "Storm", 1, "h", 1.0); store.set_playlist_tracks(survivor, [t])
    gone = store.upsert_playlist(iid, "GONE", "Storm", 1, "h", 1.0); store.set_playlist_tracks(gone, [t])
    # remote: SURV has the track, GONE no longer exists (deleted) -> cannot serve as the kept copy
    client = FakeClient(tracks={"SURV": [_track("v1", "Song", "X")]})
    c = _client(store, lambda: {iid: client})
    r = c.post("/dupe/delete", data={"source": survivor, "target": gone})  # delete survivor, "keep" the gone one
    assert r.status_code == 200 and r.json()["ok"] is False
    assert client.deleted == [] and store.get_playlist(survivor) is not None  # survivor untouched


def test_dupe_keep_one_deletes_whole_cluster(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "X", None, 1)
    # three identical copies
    ids = []
    for ytm in ("PLa", "PLb", "PLc"):
        p = store.upsert_playlist(iid, ytm, "Afternoon mix", 1, "h", 1.0); store.set_playlist_tracks(p, [t])
        ids.append((p, ytm))
    keep_id, _ = ids[0]
    client = FakeClient(tracks={ytm: [_track("v1", "Song", "X")] for _, ytm in ids})
    c = _client(store, lambda: {iid: client})
    r = c.post("/dupe/keep-one", data={"keep": keep_id})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"   # success -> recompute page
    remaining = {p.ytm_playlist_id for p in store.get_playlists()}
    assert remaining == {"PLa"}                      # only the kept copy survives
    assert sorted(client.deleted) == ["PLb", "PLc"]


def test_delete_empty_playlist(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    empty = store.upsert_playlist(iid, "PLempty", "jazz 2", 0, "h", 1.0)      # no tracks set
    client = FakeClient()                                                     # get_playlist -> {tracks: []}
    c = _client(store, lambda: {iid: client})
    r = c.post("/playlist/delete-empty", data={"playlist": empty})
    assert r.status_code == 200 and r.text.strip() == ""          # empty -> htmx removes the row
    assert client.deleted == ["PLempty"] and store.get_playlist(empty) is None

def test_delete_empty_refuses_if_not_empty(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    pl = store.upsert_playlist(iid, "PLfull", "actually full", 1, "h", 1.0)
    client = FakeClient(tracks={"PLfull": [_track("v1", "S", "X")]})          # has a track remotely
    c = _client(store, lambda: {iid: client})
    r = c.post("/playlist/delete-empty", data={"playlist": pl})
    assert r.status_code == 422 and client.deleted == []          # refused -> error toast, nothing deleted


def test_overlap_ignore_excludes_playlist(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fav = store.upsert_playlist(iid, "PLFAV", "Big Mixtape", 3, "h", 1.0)
    a = store.upsert_playlist(iid, "PLa", "A", 1, "h", 1.0)
    b = store.upsert_playlist(iid, "PLb", "B", 1, "h", 1.0)
    t1 = store.upsert_track("v1", "S1", "X", None, 1); t2 = store.upsert_track("v2", "S2", "X", None, 1)
    store.set_playlist_tracks(fav, [t1, t2]); store.set_playlist_tracks(a, [t1]); store.set_playlist_tracks(b, [t2])
    c = _client(store, lambda: {iid: FakeClient()})
    assert "Big Mixtape" in c.get("/cleanup").text                       # overlaps present
    r = c.post("/overlaps/ignore", data={"ytm": "PLFAV"})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    body = c.get("/cleanup").text
    # overlaps card spans from its header to the next section header
    overlaps_card = body.split('id="overlaps"')[1].split('<h2 class="section')[0]
    assert "Big Mixtape" not in overlaps_card                     # gone from overlaps table
    assert "Ignored in overlaps" in body                          # shows in ignored section
    c.post("/overlaps/unignore", data={"ytm": "PLFAV"}, follow_redirects=False)
    assert "Big Mixtape" in c.get("/cleanup").text                       # back

def test_system_playlist_excluded_from_overlaps(store):
    from yt_playlist.analysis import find_overlaps
    iid = store.upsert_identity("main", "cred", None, True)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 2, "h", 1.0)   # system
    a = store.upsert_playlist(iid, "PLa", "A", 1, "h", 1.0)
    t1 = store.upsert_track("v1", "S1", "X", None, 1); t2 = store.upsert_track("v2", "S2", "X", None, 1)
    store.set_playlist_tracks(lm, [t1, t2]); store.set_playlist_tracks(a, [t1])
    assert find_overlaps(store) == []   # Liked Music never generates overlaps


def test_merge_apply_route(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "A", 1, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "B", 1, "h", 1.0)
    client = FakeClient(tracks={"PLA": [_track("v1", "One", "X")], "PLB": [_track("v2", "Two", "X")]})
    c = _client(store, lambda: {iid: client})
    r = c.post("/merge/apply", data={"ids": f"{a},{b}", "result": "v1,v2", "keep": str(a)})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.deleted == ["PLB"] and store.get_playlist(b) is None

def test_merge_editor_renders_nway(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Mix", 2, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "Mix", 2, "h", 1.0)
    t1 = store.upsert_track("v1", "One", "X", None, 1); t2 = store.upsert_track("v2", "Two", "X", None, 1)
    store.set_playlist_tracks(a, [t1, t2]); store.set_playlist_tracks(b, [t1])
    c = _client(store, lambda: {iid: FakeClient()})
    body = c.get(f"/merge?ids={a},{b}").text
    assert "mergeEditor(" in body and "Where should the result go" in body
    # /dupe redirects into the same editor
    assert c.get(f"/dupe/{a}/{b}").status_code == 200


def test_undo_via_dupe_delete(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "X", None, 1)
    keep = store.upsert_playlist(iid, "KEEP", "Mix", 1, "h", 1.0); store.set_playlist_tracks(keep, [t])
    dele = store.upsert_playlist(iid, "DEL", "Mix", 1, "h", 1.0); store.set_playlist_tracks(dele, [t])
    client = FakeClient(tracks={"KEEP": [_track("v1", "Song", "X")], "DEL": [_track("v1", "Song", "X")]},
                        catalog={"v1": _track("v1", "Song", "X")})
    c = _client(store, lambda: {iid: client})
    assert c.post("/dupe/delete", data={"source": dele, "target": keep}).json()["ok"] is True
    aid = store.get_actions()[0].id
    assert "/undo/" in c.get("/actions").text                       # undo offered for the executed delete
    r = c.post(f"/undo/{aid}", follow_redirects=False)
    assert r.status_code == 303
    assert store.get_action(aid).status == "undone" and client.created  # DEL recreated from backup


def test_overlap_keep_only_mutes_others(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fav = store.upsert_playlist(iid, "PLFAV", "Favorite Songs", 3, "h", 1.0)
    other = store.upsert_playlist(iid, "PLB", "Favorite Songs 2", 2, "h", 1.0)
    noise = store.upsert_playlist(iid, "PLn", "Little Mix", 1, "h", 1.0)
    t = [store.upsert_track(f"v{i}", f"S{i}", "X", None, 1) for i in range(3)]
    store.set_playlist_tracks(fav, [t[0], t[1], t[2]]); store.set_playlist_tracks(other, [t[0], t[1]])
    store.set_playlist_tracks(noise, [t[2]])   # shares only with PLFAV (t2), not PLB
    c = _client(store, lambda: {iid: FakeClient()})
    # mute PLFAV's other overlaps but keep the PLFAV–PLB pair
    r = c.post("/overlaps/mute-others", data={"a": "PLFAV", "b": "PLB"})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    # scope to the overlaps card only (a 1-track playlist also shows under "Tiny playlists")
    overlaps_card = c.get("/cleanup").text.split('id="overlaps"')[1].split('<h2 class="section')[0]
    assert "Favorite Songs 2" in overlaps_card     # kept pair still shown
    assert "Little Mix" not in overlaps_card       # the noise overlap is muted


def test_playlists_tab_renders(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLA", "Alpha", 3, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})
    page = c.get("/playlists").text                                # Playlists moved off / to /playlists
    assert "All playlists" in page and "playlistsTab(" in page
    assert c.get("/cleanup").status_code == 200                    # cleanup moved here
    assert 'href="/cleanup"' in page                              # nav points at it
    assert c.get("/").status_code == 200                          # / is now the Home tab


def test_find_and_add_alternate_versions(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "My Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "Song A", "Artist X", "Alb", 200, 1)])
    results = [
        {"videoId": "v0", "title": "Song A", "artists": [{"name": "Artist X"}], "duration_seconds": 200},
        {"videoId": "v1", "title": "Song A (Live)", "artists": [{"name": "Artist X"}], "duration": "4:10"},
        {"videoId": "v2", "title": "Song A (Remix)", "artists": [{"name": "DJ Z"}], "duration_seconds": 190},
    ]
    fc = FakeClient(search_results=results)
    c = _client(store, lambda: {iid: fc})

    # search excludes the source track and de-dupes across the songs/videos passes
    r = c.get(f"/playlist/{a}/alternates?video_id=v0").json()
    assert r["ok"] and [x["videoId"] for x in r["results"]] == ["v1", "v2"]
    assert r["results"][0]["duration"] == 250          # "4:10" parsed to seconds

    # adding the chosen alternates appends them to the playlist (YT + store) and bumps the count
    chosen = [x for x in r["results"] if x["videoId"] in ("v1", "v2")]
    add = c.post(f"/playlist/{a}/add-tracks", json={"tracks": chosen}).json()
    assert add == {"ok": True, "added": 2, "skipped": 0, "count": 3}
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v0", "v1", "v2"]
    assert store.get_playlist(a).track_count == 3
    assert fc.added == [("PL1", ["v1", "v2"])]

    # empty selection is rejected
    assert c.post(f"/playlist/{a}/add-tracks", json={"tracks": []}).json()["ok"] is False


def test_added_alternate_keeps_album_link(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 0, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})
    track = {"videoId": "v9", "title": "T", "artist": "A", "album": "The Album",
             "album_browse": "MPREb_123", "duration": 200, "thumbnail": ""}
    assert c.post(f"/playlist/{a}/add-tracks", json={"tracks": [track]}).json()["ok"]
    detail = store.playlist_tracks_detail(a)
    assert detail[0]["album_browse"] == "MPREb_123"   # album becomes a browse link in the view


def test_enrich_playlist_via_musicbrainz(store, monkeypatch):
    import json as _json
    import yt_playlist.musicbrainz as mb
    # stub MusicBrainz so the test never hits the network
    monkeypatch.setattr(mb, "enrich",
                        lambda title, artist: {"S0": ("rock", "1998"), "S1": ("jazz", "2003")}.get(title, (None, None)))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 3, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S0", "X", "Al", 200, 1),
                                  store.upsert_track("v1", "S1", "Y", "Al", 200, 1),
                                  store.upsert_track("v2", "Sx", "Z", "Al", 200, 1)])
    c = _client(store, lambda: {iid: FakeClient()})

    assert len(store.tracks_to_enrich(a)) == 3
    jid = c.post(f"/playlist/{a}/enrich/musicbrainz").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid}") as st:
        body = "".join(st.iter_text())
    types = [_json.loads(l[6:])["type"] for l in body.splitlines() if l.startswith("data: ")]
    assert types == ["info", "track", "track", "track", "done", "end"]

    detail = {t["video_id"]: (t["genre"], t["year"]) for t in store.playlist_tracks_detail(a)}
    assert detail["v0"] == ("rock", "1998") and detail["v1"] == ("jazz", "2003")
    assert detail["v2"] == ("", "")                      # no match this run
    # fully-resolved tracks are done; the no-match one stays eligible for a re-run
    assert [t["video_id"] for t in store.tracks_to_enrich(a)] == ["v2"]

    # re-run after MusicBrainz gains a genre for v2 — it fills the gap and v2 is now complete
    monkeypatch.setattr(mb, "enrich", lambda title, artist: ("ambient", "2010") if title == "Sx" else (None, None))
    jid2 = c.post(f"/playlist/{a}/enrich/musicbrainz").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid2}") as st:
        "".join(st.iter_text())
    v2 = next(t for t in store.playlist_tracks_detail(a) if t["video_id"] == "v2")
    assert (v2["genre"], v2["year"]) == ("ambient", "2010")
    assert store.tracks_to_enrich(a) == []

    # a blank result must never clobber values we already have
    store.set_track_enrichment(store.track_ids_for_videos(["v0"])["v0"], "", "")
    v0 = next(t for t in store.playlist_tracks_detail(a) if t["video_id"] == "v0")
    assert (v0["genre"], v0["year"]) == ("rock", "1998")


def test_liked_songs_get_a_heart(store):
    iid = store.upsert_identity("main", "cred", None, True)
    mix = store.upsert_playlist(iid, "PLM", "Mix", 2, "h", 1.0)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 1, "h", 1.0)   # the YouTube liked playlist
    loved = store.upsert_track("v1", "Loved", "A", "Al", 200, 1)
    store.set_playlist_tracks(mix, [loved, store.upsert_track("v2", "Plain", "B", "Al", 200, 1)])
    store.set_playlist_tracks(lm, [loved])

    liked = {t["video_id"]: t["liked"] for t in store.playlist_tracks_detail(mix)}
    assert liked == {"v1": True, "v2": False}                       # song in LM is liked
    assert store.artist_songs("A")[0]["liked"] is True             # and wherever the song appears
    c = _client(store, lambda: {iid: FakeClient()})
    assert "liked-heart" in c.get(f"/playlist/{mix}").text


def test_remove_playlist_preserves_group(store):
    # Groups are user curation (not on YouTube) — a removed/pruned playlist must keep its group so it
    # reattaches if the playlist comes back. Regression for the prune that wiped groups too.
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLG", "Grouped", 1, "h", 1.0)
    store.set_playlist_group("PLG", "Workout")
    store.remove_playlist(a)
    assert store.get_playlist_groups() == {"PLG": "Workout"}      # survives removal
    store.upsert_playlist(iid, "PLG", "Grouped", 1, "h", 2.0)     # re-added on a later sync
    assert store.get_playlist_groups().get("PLG") == "Workout"    # grouping reattaches


def test_sync_keeps_playlists_when_library_comes_back_empty(store):
    # Regression: an empty get_library_playlists (session glitch, not a 401) must NOT prune the store.
    from yt_playlist import sync as sync_mod
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Keep Me", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S", "X", None, None, 1)])
    sync_mod.sync_identity(store, iid, FakeClient(playlists=[], tracks={}, history=[]), now=2.0)
    assert [p.title for p in store.get_playlists()] == ["Keep Me"]      # preserved, not wiped


def test_sync_flags_reauth_when_known_identity_returns_empty(store):
    # An identity that HAD playlists but now returns empty is flagged for re-auth (not silently empty).
    from yt_playlist import sync as sync_mod
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PL1", "Keep Me", 1, "h", 1.0)
    expired = {}
    sync_mod.sync_identity(store, iid, FakeClient(playlists=[], tracks={}, history=[]), now=2.0,
                           label="main", on_auth_expired=lambda i, label: expired.__setitem__(i, label))
    assert expired == {iid: "main"}                    # flagged for re-authentication
    assert [p.title for p in store.get_playlists()] == ["Keep Me"]   # still preserved


def test_sync_prunes_when_library_is_real(store):
    # A genuine, non-empty library still prunes playlists that are actually gone.
    from yt_playlist import sync as sync_mod
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "GONE", "Deleted Remotely", 0, "h", 1.0)
    client = FakeClient(playlists=[{"playlistId": "PL2", "title": "New", "count": 1}],
                        tracks={"PL2": [_track("v1", "A", "X")]}, history=[])
    sync_mod.sync_identity(store, iid, client, now=2.0)
    assert sorted(p.title for p in store.get_playlists()) == ["New"]    # GONE pruned, New added


def test_resignin_clears_banner_and_flashes(store):
    rt = _FakeRuntime(store, configured=True)
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    app.state.ctx.auth_expired["main"] = "Main"          # simulate an expired session (banner shown)
    c = TestClient(app, base_url="http://127.0.0.1")

    r = c.post("/setup", data={"headers": "Cookie: SID=abc", "label": ["main"],
                               "brand_account_id": [""], "master": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?flash=Signed%20back%20in."   # re-signin message, back to playlists
    assert app.state.ctx.auth_expired == {}                          # banner cleared


def test_jobs_find_active():
    from yt_playlist.web.jobs import SyncJobs
    jobs = SyncJobs()
    j1 = jobs.create(); j1.playlist_id = 5; j1.source = "musicbrainz"
    j2 = jobs.create(); j2.playlist_id = 5; j2.source = "discogs"
    assert jobs.find_active(5) is j2            # most recent running job for the playlist
    assert jobs.find_active(9) is None
    j2.done = True
    assert jobs.find_active(5) is j1            # falls back to the still-running older one
    j1.done = True
    assert jobs.find_active(5) is None          # nothing running -> no rejoin


def test_page_rejoins_active_enrichment(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S", "X", "Al", 200, 1)])
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")

    assert f"enrichPanel({a}, false, 0, '')" in c.get(f"/playlist/{a}").text   # nothing to rejoin
    job = app.state.ctx.jobs.create(); job.playlist_id = a; job.source = "discogs"
    page = c.get(f"/playlist/{a}").text
    assert f"enrichPanel({a}, false, {job.id}, 'discogs')" in page and "rejoinIfActive()" in page
    job.done = True
    assert f"enrichPanel({a}, false, 0, '')" in c.get(f"/playlist/{a}").text   # finished -> no rejoin


def test_discogs_fills_genre_and_year(store, monkeypatch):
    import json as _json
    import yt_playlist.discogs as dc
    # stub Discogs search: styles map to the whitelist, earliest year wins
    monkeypatch.setattr(dc, "_search", lambda q, tok: [
        {"year": "1996", "genre": ["Electronic"], "style": ["Techno", "Drum n Bass"]},
        {"year": "1995", "genre": ["Electronic"], "style": ["Techno"]},
    ] if "Born Slippy" in q else [])
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 2, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "Born Slippy", "Underworld", "Al", 200, 1),
                                  store.upsert_track("v1", "Unknown", "Nobody", "Al", 200, 1)])
    c = _client(store, lambda: {iid: FakeClient()})

    jid = c.post(f"/playlist/{a}/enrich/discogs").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid}") as st:
        body = "".join(st.iter_text())
    types = [_json.loads(l[6:])["type"] for l in body.splitlines() if l.startswith("data: ")]
    assert types == ["info", "track", "track", "done", "end"]
    detail = {t["video_id"]: (t["genre"], t["year"]) for t in store.playlist_tracks_detail(a)}
    assert detail["v0"] == ("Techno", "1995")        # style->genre, earliest year
    assert detail["v1"] == ("", "")


def test_lastfm_fills_missing_genre_and_year(store, monkeypatch):
    import json as _json
    import yt_playlist.lastfm as lf
    monkeypatch.setenv("LASTFM_API_KEY", "testkey")
    # stub the Last.fm lookup: (genre, year) per track; one fully unknown
    monkeypatch.setattr(lf, "enrich", lambda title, artist, key:
                        {"S0": ("Trip Hop", None), "S1": ("House", "2010")}.get(title, (None, None)))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 3, "h", 1.0)
    t0 = store.upsert_track("v0", "S0", "X", "Al", 200, 1)
    store.set_playlist_tracks(a, [t0, store.upsert_track("v1", "S1", "Y", "Al", 200, 1),
                                  store.upsert_track("v2", "S?", "Z", "Al", 200, 1)])
    store.set_track_year(t0, "1998")                 # fill-only must not clobber this
    c = _client(store, lambda: {iid: FakeClient()})

    jid = c.post(f"/playlist/{a}/enrich/lastfm").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid}") as st:
        body = "".join(st.iter_text())
    evs = [_json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
    track_evs = [e for e in evs if e["type"] == "track"]
    # events report the effective stored values (so the UI matches the DB) — both fields present
    assert track_evs and all("genre" in e and "year" in e for e in track_evs)

    detail = {t["video_id"]: (t["genre"], t["year"]) for t in store.playlist_tracks_detail(a)}
    assert detail["v0"] == ("Trip Hop", "1998")      # genre filled, existing year preserved
    assert detail["v1"] == ("House", "2010")         # both filled from Last.fm
    assert detail["v2"] == ("", "")                  # nothing found -> still missing


def test_lastfm_enrich_scrapes_release_year(monkeypatch):
    import yt_playlist.lastfm as lf
    # one getInfo gives tags + the album page URL; the album page carries the Release Date
    monkeypatch.setattr(lf, "_get", lambda params: {"track": {
        "album": {"url": "https://www.last.fm/music/Ph+1/Sizzling+Love"},
        "toptags": {"tag": [{"name": "seen live"}, {"name": "house"}]}}})
    fetched = []
    def fake_fetch(url):
        fetched.append(url)
        return ('<dt class="catalogue-metadata-heading">Release Date</dt>'
                '<dd class="catalogue-metadata-description">11 March 1996</dd>')
    monkeypatch.setattr(lf, "_fetch_text", fake_fetch)
    assert lf.enrich("Sizzling Love", "Ph 1", "key") == ("House", "1996")
    assert fetched == ["https://www.last.fm/music/Ph+1/Sizzling+Love"]   # the album page, not the track


def test_lastfm_key_saved_via_ui(store, monkeypatch, tmp_path):
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))   # no env/config key
    import yt_playlist.lastfm as lf
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})

    # the playlist page reflects "not configured"; after saving a key it flips to configured
    assert lf.api_key(store) is None
    assert "enrichPanel(%d, false," % a in c.get(f"/playlist/{a}").text
    r = c.post("/settings/lastfm-key", json={"key": " abc123 "}).json()
    assert r == {"ok": True, "configured": True}
    assert store.get_setting("lastfm_api_key") == "abc123" and lf.api_key(store) == "abc123"
    assert "enrichPanel(%d, true," % a in c.get(f"/playlist/{a}").text


def test_lastfm_without_key_reports_error(store, monkeypatch, tmp_path):
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))   # empty config -> no key
    import json as _json
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S0", "X", "Al", 200, 1)])
    c = _client(store, lambda: {iid: FakeClient()})
    jid = c.post(f"/playlist/{a}/enrich/lastfm").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid}") as st:
        body = "".join(st.iter_text())
    assert "Last.fm API key" in body


def test_set_track_year(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S0", "X", "Al", 200, 1)])
    c = _client(store, lambda: {iid: FakeClient()})
    assert c.post(f"/playlist/{a}/track-year", json={"video_id": "v0", "year": "1991"}).json()["ok"]
    assert store.playlist_tracks_detail(a)[0]["year"] == "1991"


def test_set_track_genre_and_suggestions(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 2, "h", 1.0)
    t0 = store.upsert_track("v0", "S0", "X", "Al", 200, 1)
    store.set_playlist_tracks(a, [t0, store.upsert_track("v1", "S1", "Y", "Al", 200, 1)])
    store.set_track_genre(t0, "Rock")
    c = _client(store, lambda: {iid: FakeClient()})

    # autosuggest list = distinct genres so far, alpha-sorted; rendered into a datalist
    assert store.all_genres() == ["Rock"]
    page = c.get(f"/playlist/{a}").text
    assert 'id="genrelist"' in page and 'value="Rock"' in page and "startEditGenre" in page

    # set a genre on the second track; it persists and joins the suggestion list
    assert c.post(f"/playlist/{a}/track-genre", json={"video_id": "v1", "genre": "Jazz"}).json()["ok"]
    detail = {t["video_id"]: t["genre"] for t in store.playlist_tracks_detail(a)}
    assert detail["v1"] == "Jazz"
    assert store.all_genres() == ["Jazz", "Rock"]


def test_reorder_and_remove_track(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 3, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track(f"v{i}", f"S{i}", "X", "Alb", 200, 1) for i in range(3)])
    # the YT playlist exposes setVideoIds (what moves/removes are keyed on)
    fc = FakeClient(tracks={"PL1": [{"videoId": f"v{i}", "setVideoId": f"sv{i}"} for i in range(3)]})
    c = _client(store, lambda: {iid: fc})

    # move v2 before v0 (to the top)
    assert c.post(f"/playlist/{a}/reorder", json={"video_id": "v2", "before_video_id": "v0"}).json()["ok"]
    assert fc.edited[-1] == ("PL1", {"moveItem": ("sv2", "sv0")})
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v2", "v0", "v1"]

    # move v2 to the end (empty successor -> bare setVideoId)
    c.post(f"/playlist/{a}/reorder", json={"video_id": "v2", "before_video_id": ""})
    assert fc.edited[-1] == ("PL1", {"moveItem": "sv2"})
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v0", "v1", "v2"]

    # remove v1
    assert c.post(f"/playlist/{a}/remove-track", json={"video_id": "v1"}).json() == {"ok": True, "count": 2}
    assert fc.removed == [("PL1", [{"videoId": "v1", "setVideoId": "sv1"}])]
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v0", "v2"]
    assert store.get_playlist(a).track_count == 2


def test_toasts_region_present_on_every_page(store):
    store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert 'id="toasts"' in c.get("/discover").text


def test_error_toast_partial_renders(store):
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    tmpl = app.state.ctx.templates.env.get_template("_partials/error_toast.html")
    html = tmpl.render(message="Boom")
    assert "Boom" in html
    assert 'hx-swap-oob="afterbegin:#toasts"' in html


def test_no_stalerow_alpine_remains(store):
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert "staleRow" not in c.get("/discover").text


def _two_identity_move(store):
    i1 = store.upsert_identity("Main", "c1", None, True)
    i2 = store.upsert_identity("Alt", "c2", None, False)
    pid = store.upsert_playlist(i1, "PL1", "Mix", 1, "h", 1.0)
    t = store.upsert_track("v1", "S", "X", None, None, 1)
    store.set_playlist_tracks(pid, [t])
    return i1, i2, pid


def test_move_run_copy_returns_row_with_message(store):
    i1, i2, pid = _two_identity_move(store)
    c = _client(store, lambda: {i1: FakeClient(), i2: FakeClient()})
    r = c.post("/move/run", data={"playlist": pid, "target_identity": i2, "copy_only": "1"})
    assert r.status_code == 200
    assert "Copied" in r.text and "Mix" in r.text       # row re-rendered, still present
    assert store.get_playlist(pid) is not None


def test_move_run_move_removes_row(store):
    i1, i2, pid = _two_identity_move(store)
    c = _client(store, lambda: {i1: FakeClient(), i2: FakeClient()})
    r = c.post("/move/run", data={"playlist": pid, "target_identity": i2})   # move (no copy_only)
    assert r.status_code == 200
    assert r.text.strip() == ""                          # deleted -> empty -> htmx removes the row
    assert store.get_playlist(pid) is None


def test_move_run_same_identity_shows_inline_error(store):
    i1, _i2, pid = _two_identity_move(store)
    c = _client(store, lambda: {i1: FakeClient()})
    r = c.post("/move/run", data={"playlist": pid, "target_identity": i1})
    assert r.status_code == 200
    assert "same" in r.text.lower() and "move-row" in r.text   # error re-rendered in the row


def test_delete_empty_removes_row(store):
    iid = store.upsert_identity("main", "cred", None, True)
    pid = store.upsert_playlist(iid, "PLE", "Empty One", 0, "h", 1.0)   # no tracks
    c = _client(store, lambda: {iid: FakeClient()})
    assert c.get("/cleanup").text.count('class="empty-row"') == 1
    r = c.post("/playlist/delete-empty", data={"playlist": pid})
    assert r.status_code == 200 and r.text.strip() == ""       # empty -> htmx removes the row
    assert store.get_playlist(pid) is None


def test_unhide_overlap_restores_to_overlaps_section(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Alpha", 3, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "Beta", 3, "h", 1.0)
    t = [store.upsert_track(f"v{i}", f"S{i}", "X", None, None, 1) for i in range(4)]
    store.set_playlist_tracks(a, [t[0], t[1], t[2]])           # {0,1,2}
    store.set_playlist_tracks(b, [t[1], t[2], t[3]])           # {1,2,3}: shared {1,2}, jaccard .5 -> overlap
    c = _client(store, lambda: {iid: FakeClient()})

    store.suppress_overlap("PLA", "PLB", 1.0)                  # hide the pair
    assert c.get("/cleanup").text.count('class="ov-row"') == 0  # gone from Overlaps, now Hidden

    r = c.post("/overlaps/unsuppress", data={"a": "PLA", "b": "PLB"})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"   # recompute, not drop-in-place
    assert c.get("/cleanup").text.count('class="ov-row"') == 1  # restored to the Overlaps section


def test_keep_one_refreshes_and_deletes_other_copies(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Dup A", 2, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "Dup B", 2, "h", 1.0)
    t = [store.upsert_track(f"d{i}", f"D{i}", "X", None, None, 1) for i in range(2)]
    store.set_playlist_tracks(a, t); store.set_playlist_tracks(b, t)   # identical -> a dup group
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post("/dupe/keep-one", data={"keep": a})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    assert store.get_playlist(a) is not None and store.get_playlist(b) is None   # other copy deleted
