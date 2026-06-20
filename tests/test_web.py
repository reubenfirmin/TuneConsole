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

def test_rediscover_dismiss_and_restore(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLZ", "Old Mix", 3, "h", 0.0)
    c = _client(store, lambda: {iid: FakeClient()})

    assert "Rediscover" in c.get("/discover").text
    # candidate appears as an actionable row
    assert "PLZ" in c.get("/discover").text

    r = c.post("/rediscover/dismiss", data={"ytm": "PLZ"})
    assert r.status_code == 200 and r.json()["ok"]
    body = c.get("/discover").text
    # no longer a candidate row, but shown in the snoozed/dismissed section
    assert body.count("staleRow()") == 0
    assert "dismissed" in body and "PLZ" in body

    r = c.post("/rediscover/restore", data={"ytm": "PLZ"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/discover").text.count("staleRow()") == 1

def test_rediscover_snooze_expires(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLZ", "Old Mix", 3, "h", 0.0)
    # now_fn = 1.0; snooze 30d hides it now...
    c = _client(store, lambda: {iid: FakeClient()})
    assert c.post("/rediscover/snooze", data={"ytm": "PLZ", "days": "30"}).json()["ok"]
    assert c.get("/discover").text.count("staleRow()") == 0
    # ...but a far-future snooze is treated as expired (no longer hidden)
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
    body = c.get("/").text
    assert "unpkg.com" not in body and "cdn.jsdelivr" not in body
    assert "/static/vendor/htmx.min.js" in body


def test_setup_check_reports_account(store):
    rt = _FakeRuntime(store); rt.account_name = "Reuben"
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/setup/check", data={"headers": "cookie: x"})
    assert r.status_code == 200 and r.json() == {"ok": True, "account": "Reuben"}

def test_setup_check_reports_error(store):
    rt = _FakeRuntime(store); rt.check_error = "sign-in didn't work (401)"
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/setup/check", data={"headers": "bad"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "sign-in" in body["error"]


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
    assert "Little Mix" in c.get("/").text
    r = c.post("/overlaps/suppress", data={"a": "PLFAV", "b": "PLSML"})
    assert r.status_code == 200 and r.json()["ok"] is True   # AJAX, no redirect
    # check the section header specifically (other copy may mention "hidden")
    assert 'Hidden overlaps <span class="count">' in c.get("/").text
    c.post("/overlaps/unsuppress", data={"a": "PLSML", "b": "PLFAV"})
    assert 'Hidden overlaps <span class="count">' not in c.get("/").text


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
    assert r.status_code == 200 and r.json() == {"ok": True, "n": 2}
    assert "Hidden overlaps" in c.get("/").text
    # malformed entries are ignored, not fatal
    r = c.post("/overlaps/suppress-many", data={"pairs": json.dumps([["only-one"], "junk", []])})
    assert r.status_code == 200 and r.json()["n"] == 0


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
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["deleted"] == 2
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
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.deleted == ["PLempty"] and store.get_playlist(empty) is None

def test_delete_empty_refuses_if_not_empty(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    pl = store.upsert_playlist(iid, "PLfull", "actually full", 1, "h", 1.0)
    client = FakeClient(tracks={"PLfull": [_track("v1", "S", "X")]})          # has a track remotely
    c = _client(store, lambda: {iid: client})
    r = c.post("/playlist/delete-empty", data={"playlist": pl})
    assert r.json()["ok"] is False and client.deleted == []


def test_overlap_ignore_excludes_playlist(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fav = store.upsert_playlist(iid, "PLFAV", "Big Mixtape", 3, "h", 1.0)
    a = store.upsert_playlist(iid, "PLa", "A", 1, "h", 1.0)
    b = store.upsert_playlist(iid, "PLb", "B", 1, "h", 1.0)
    t1 = store.upsert_track("v1", "S1", "X", None, 1); t2 = store.upsert_track("v2", "S2", "X", None, 1)
    store.set_playlist_tracks(fav, [t1, t2]); store.set_playlist_tracks(a, [t1]); store.set_playlist_tracks(b, [t2])
    c = _client(store, lambda: {iid: FakeClient()})
    assert "Big Mixtape" in c.get("/").text                       # overlaps present
    r = c.post("/overlaps/ignore", data={"ytm": "PLFAV"})
    assert r.status_code == 200 and r.json()["ok"] is True
    body = c.get("/").text
    # overlaps card spans from its header to the next section header
    overlaps_card = body.split('id="overlaps"')[1].split('<h2 class="section')[0]
    assert "Big Mixtape" not in overlaps_card                     # gone from overlaps table
    assert "Ignored in overlaps" in body                          # shows in ignored section
    c.post("/overlaps/unignore", data={"ytm": "PLFAV"}, follow_redirects=False)
    assert "Big Mixtape" in c.get("/").text                       # back

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
    r = c.post("/overlaps/ignore-except", data={"ytm": "PLFAV", "a": "PLFAV", "b": "PLB"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # scope to the overlaps card only (a 1-track playlist also shows under "Tiny playlists")
    overlaps_card = c.get("/").text.split('id="overlaps"')[1].split('<h2 class="section')[0]
    assert "Favorite Songs 2" in overlaps_card     # kept pair still shown
    assert "Little Mix" not in overlaps_card       # the noise overlap is muted
