import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.library import executor
from yt_playlist.rec import radio as radio_mod
from yt_playlist.rec.radio import RadioSession
from yt_playlist.web.routes import bridge as bridge_route

_TEMPLATES_DIR = "src/yt_playlist/web/templates"


class _FakeBridge:
    def __init__(self, connected=True):
        self.connected = connected
        self.now_playing = None
        self.sent = []

    def send_control(self, payload):
        self.sent.append(payload)


class _FakeClient:
    pass


class _Ctx:
    def __init__(self, store, bridge, radio, client):
        self.store = store; self.bridge = bridge; self.radio = radio
        self.now_fn = lambda: 1000.0
        self.client_provider = lambda: ({"id1": client} if client else {})
        self.templates = Jinja2Templates(directory=_TEMPLATES_DIR)


def _pool_pick_next(pool):
    # Exclusion-AWARE fake (unlike a plain exhausting iterator): /radio/start now attempts the dual
    # seed first, which -- with only enough picks for ONE deck -- seeds deck A, fails to seed deck B,
    # and unwinds via session.reset() (clearing every exclusion) before falling back to the v2
    # single-tab seed within the SAME request. A plain `iter(...)` stub would already be exhausted by
    # deck A's consumption and starve that fallback; keying off the real `radio_mod._exclusions(se)`
    # (as start_dual_session/rebuild_standby's own tests do) instead makes a reset visible again, same
    # as the real DB-backed picker would behave.
    def _pick(st, se, now):
        excl = radio_mod._exclusions(se)
        for p in pool:
            if p["key"] not in excl:
                return p
        return None
    return _pick


_DEFAULT_PICK_POOL = [{"key": "k1", "video_id": "v1", "artist": "A", "title": "t", "url": "u"},
                      {"key": "k2", "video_id": "v2", "artist": "B", "title": "t", "url": "u"},
                      {"key": "k3", "video_id": "v3", "artist": "C", "title": "t", "url": "u"}]


@pytest.fixture
def client(monkeypatch):
    store = Store(":memory:"); store.init_schema()
    bridge = _FakeBridge(); ctx = _Ctx(store, bridge, RadioSession(), _FakeClient())
    # Deterministic picks so start_session seeds without a built model. Exactly enough for ONE deck
    # (radio_deck_size default 3), so the dual attempt always fails to seed a disjoint deck B and
    # falls back to the v2 single-tab path -- matching every pre-dual test below, unless a test
    # overrides this with a bigger pool (see test_start_dual_* further down).
    monkeypatch.setattr(radio_mod, "pick_next", _pool_pick_next(_DEFAULT_PICK_POOL))
    # Stub the remote create + reconcile (no real extension/account).
    monkeypatch.setattr(executor, "create_generated_playlist",
                        lambda *a, **k: {"new_ytm": "PLRADIO", "pid": 1})
    monkeypatch.setattr(executor, "_reconcile", lambda *a, **k: (0, 0, [], []))
    app = FastAPI(); app.include_router(bridge_route.build(ctx))
    return TestClient(app), ctx, bridge


def test_start_creates_seeds_and_navigates_into_playlist(client):
    tc, ctx, bridge = client
    assert tc.post("/radio/start").json() == {"ok": True}
    assert ctx.radio.active is True
    assert ctx.store.get_setting("radio_playlist_ytm") == "PLRADIO"
    assert ctx.store.get_setting("radio_active") == "1"
    nav = next(f for f in bridge.sent if f["type"] == "navigate")
    assert nav["url"] == "https://music.youtube.com/watch?v=v1&list=PLRADIO"   # into OUR playlist
    prime = next(f for f in bridge.sent if f["type"] == "radio-prime")
    assert prime["url"] == "https://music.youtube.com/watch?v=v2&list=PLRADIO"
    assert tc.get("/bridge/status").json()["radio"] is True


def test_start_not_available_when_no_client(client):
    tc, ctx, bridge = client
    ctx.client_provider = lambda: {}
    assert tc.post("/radio/start").json()["ok"] is False
    assert ctx.radio.active is False


def test_start_not_available_when_disconnected(client):
    tc, ctx, bridge = client
    bridge.connected = False
    r = tc.post("/radio/start").json()
    assert r == {"ok": False, "reason": "not available"}
    assert ctx.radio.active is False


def test_start_not_available_when_no_pick(client, monkeypatch):
    # start_session seeds via pick_next; when nothing is pickable at all, it fails open.
    tc, ctx, bridge = client
    monkeypatch.setattr(radio_mod, "pick_next", lambda st, se, now: None)
    assert tc.post("/radio/start").json() == {"ok": False, "reason": "not available"}
    assert ctx.radio.active is False


def test_start_reuses_existing_playlist(client, monkeypatch):
    # A radio_playlist_ytm already on record (and still present in the store's playlists) must be
    # reconciled to the new seeds rather than recreating a fresh playlist.
    tc, ctx, bridge = client
    ctx.store.set_setting("radio_playlist_ytm", "PLOLD")
    monkeypatch.setattr(ctx.store, "get_playlists", lambda: [
        type("P", (), {"ytm_playlist_id": "PLOLD"})()])
    calls = []
    monkeypatch.setattr(executor, "_reconcile", lambda *a, **k: calls.append(a) or (0, 0, [], []))
    assert tc.post("/radio/start").json() == {"ok": True}
    assert ctx.store.get_setting("radio_playlist_ytm") == "PLOLD"
    assert calls   # _reconcile was used instead of create_generated_playlist


def test_start_dual_creates_both_decks_and_sends_deck_start(client, monkeypatch):
    # Enough picks for BOTH decks (radio_deck_size default 3 each) -> the dual attempt succeeds and
    # /radio/start never falls back to the v2 single-tab path at all.
    tc, ctx, bridge = client
    pool = [{"key": f"k{i}", "video_id": f"v{i}", "artist": chr(64 + i), "title": "t", "url": "u"}
            for i in range(1, 7)]
    monkeypatch.setattr(radio_mod, "pick_next", _pool_pick_next(pool))
    ytms = iter(["PLA", "PLB"])
    monkeypatch.setattr(executor, "create_generated_playlist",
                        lambda *a, **k: {"new_ytm": next(ytms), "pid": 1})
    assert tc.post("/radio/start").json() == {"ok": True}
    assert ctx.radio.active is True
    a = ctx.store.get_setting("radio_playlist_a_ytm")
    b = ctx.store.get_setting("radio_playlist_b_ytm")
    assert a and b and a != b
    ds = next(f for f in bridge.sent if f["type"] == "deck-start")
    assert "&list=" in ds["liveUrl"] and "&list=" in ds["standbyUrl"]
    assert ds["boundaryVideoId"]                       # the live deck's last vid, arming the toggle
    assert not any(f["type"] == "navigate" for f in bridge.sent)   # v2 fallback never ran


def test_start_dual_deck_start_carries_epoch_and_standby_boundary(client, monkeypatch):
    # C1/C2 (final review). C1: deck-start must carry the session epoch -- the extension seeds its
    # deck-toggled echo (lastDeckEpoch) from it, and without the seed a skip-free first session acks
    # its first natural toggle with epoch:null, which the WS handler's strict echo guard drops
    # (desyncing physical tabs from logical decks at the very first boundary). C2: it must also
    # carry the STANDBY deck's boundary vid so the extension can arm the promoted deck's boundary;
    # otherwise that deck's last-track end falls through to YTM autoplay (or a stall).
    tc, ctx, bridge = client
    pool = [{"key": f"k{i}", "video_id": f"v{i}", "artist": chr(64 + i), "title": "t", "url": "u"}
            for i in range(1, 7)]
    monkeypatch.setattr(radio_mod, "pick_next", _pool_pick_next(pool))
    ytms = iter(["PLA", "PLB"])
    monkeypatch.setattr(executor, "create_generated_playlist",
                        lambda *a, **k: {"new_ytm": next(ytms), "pid": 1})
    assert tc.post("/radio/start").json() == {"ok": True}
    ds = next(f for f in bridge.sent if f["type"] == "deck-start")
    assert "epoch" in ds and ds["epoch"] == ctx.radio.epoch == 0
    standby_label = "B" if ctx.radio.live_label == "A" else "A"
    assert ds["standbyBoundaryVideoId"] == ctx.radio.decks[standby_label]["boundary_vid"]
    assert ds["standbyBoundaryVideoId"]   # a real vid, not None/empty


def test_start_clears_stale_fallback_reason_at_attempt_start(client, monkeypatch):
    # Fallback diagnostics (visibility wave, Part 1): every /radio/start attempt starts with an honest
    # slate -- a reason left over from an EARLIER attempt must not keep haunting /bridge/status once a
    # later attempt succeeds.
    tc, ctx, bridge = client
    ctx.radio.fallback_reason = "stale reason from a previous attempt"
    pool = [{"key": f"k{i}", "video_id": f"v{i}", "artist": chr(64 + i), "title": "t", "url": "u"}
            for i in range(1, 7)]
    monkeypatch.setattr(radio_mod, "pick_next", _pool_pick_next(pool))
    ytms = iter(["PLA", "PLB"])
    monkeypatch.setattr(executor, "create_generated_playlist",
                        lambda *a, **k: {"new_ytm": next(ytms), "pid": 1})

    assert tc.post("/radio/start").json() == {"ok": True}

    assert ctx.radio.fallback_reason is None   # dual succeeded this time; stale reason cleared


def test_start_dual_failure_sets_server_side_fallback_reason(client):
    # Server-side fallback diagnostics (Part 1): the `client` fixture's default pick pool only seeds
    # ONE deck (see its own comment), so the dual attempt always raises "dual seed empty" and falls
    # back to the v2 single-tab path within the SAME request -- the except path must stamp WHY, same
    # as the extension's own deck-ready {fallback:true, reason} does for its side of the failure.
    tc, ctx, bridge = client

    assert tc.post("/radio/start").json() == {"ok": True}

    assert ctx.radio.dual_deck is False
    assert ctx.radio.fallback_reason is not None
    assert ctx.radio.fallback_reason.startswith("server: ")
    assert "dual seed empty" in ctx.radio.fallback_reason
    assert tc.get("/bridge/status").json()["radio_fallback_reason"] == ctx.radio.fallback_reason


def test_start_dual_aborted_by_stop_during_setup_sends_no_deck_start(client, monkeypatch):
    # M5 (final review), stop-during-start: /radio/stop lands while the dual setup's playlist
    # create/reconcile round-trips are in flight (simulated here by the create stub itself running
    # the stop's reset, network latency compressed to zero). The start continuation must NOT
    # re-populate the freshly-reset session, must NOT flip radio_active back to "1", and must NOT
    # send deck-start -- that would stand up a zombie radio window playing music the backend no
    # longer reacts to, with the UI saying stopped.
    tc, ctx, bridge = client
    pool = [{"key": f"k{i}", "video_id": f"v{i}", "artist": chr(64 + i), "title": "t", "url": "u"}
            for i in range(1, 7)]
    monkeypatch.setattr(radio_mod, "pick_next", _pool_pick_next(pool))

    def _create_then_stop(*a, **k):
        # what /radio/stop does to session state, landing mid-setup
        with ctx.radio.lock:
            ctx.radio.reset()
        ctx.store.set_setting("radio_active", "0")
        return {"new_ytm": "PLA", "pid": 1}
    monkeypatch.setattr(executor, "create_generated_playlist", _create_then_stop)

    r = tc.post("/radio/start").json()

    assert r["ok"] is False
    assert ctx.radio.active is False
    assert ctx.store.get_setting("radio_active") == "0"
    assert not any(f["type"] == "deck-start" for f in bridge.sent)
    assert not any(f["type"] == "navigate" for f in bridge.sent)   # nor a v2 fallback start


def test_start_dual_play_frame_is_inert_before_deck_ready(client, monkeypatch):
    # BINDING CORRECTION (T7h): after a successful dual start, session.dual_deck stays False until the
    # extension's "deck-ready" frame confirms both decks really exist (see bridge_ws's inbound handler);
    # start_dual_session's own provisional True is deliberately downgraded back to False by the route
    # right after seeding. Pin the interim semantics that choice implies: a `play` frame arriving in
    # that window is routed through on_play's v2 branch (it keys on dual_deck), which finds nothing in
    # session.queue -- start_dual_session never populates it, only session.decks -- and returns the
    # existing "user navigated off our queue: do not fight" no-op. The frame is DROPPED (no rebuild, no
    # reconcile, no send), not applied to either deck; neither deck's own state moves. The decks
    # themselves remain fully real and playable throughout (their mini playlists already hold the
    # seeded vids) -- only the session's own reactive bookkeeping is paused.
    tc, ctx, bridge = client
    pool = [{"key": f"k{i}", "video_id": f"v{i}", "artist": chr(64 + i), "title": "t", "url": "u"}
            for i in range(1, 7)]
    monkeypatch.setattr(radio_mod, "pick_next", _pool_pick_next(pool))
    ytms = iter(["PLA", "PLB"])
    monkeypatch.setattr(executor, "create_generated_playlist",
                        lambda *a, **k: {"new_ytm": next(ytms), "pid": 1})
    assert tc.post("/radio/start").json() == {"ok": True}
    assert ctx.radio.dual_deck is False                         # confirmation has not landed yet
    deck_a_before = list(ctx.radio.decks["A"]["queue"])
    deck_b_before = list(ctx.radio.decks["B"]["queue"])
    pos_before = ctx.radio.pos

    out = radio_mod.on_play(ctx.store, ctx.radio, "v1", ctx.now_fn())

    assert out == {"desired_vids": None, "prime": None}        # dropped, not applied
    assert ctx.radio.decks["A"]["queue"] == deck_a_before
    assert ctx.radio.decks["B"]["queue"] == deck_b_before
    assert ctx.radio.pos == pos_before
    assert ctx.radio.dual_deck is False                         # still awaiting deck-ready
    # The decks themselves stay real and untouched: their playlist ids are unaffected by the dropped
    # play frame.
    assert ctx.radio.decks["A"]["playlist_ytm"] == "PLA"
    assert ctx.radio.decks["B"]["playlist_ytm"] == "PLB"


def test_populate_single_tab_still_force_topups_live_queue(client):
    # v2 single-tab path is unchanged by dual-awareness: no dual session was ever confirmed here
    # (dual_deck stays False the whole time -- the default pool only seeds one deck, see the client
    # fixture's comment), so /radio/populate must still force a tail top-up on session.queue.
    tc, ctx, bridge = client
    tc.post("/radio/start")
    assert ctx.radio.dual_deck is False
    r = tc.post("/radio/populate").json()
    assert r["ok"] is True
    assert "queue" in r and "standby" not in r
    assert r["queue"] == [q["video_id"] for q in ctx.radio.queue]


def test_populate_dual_rebuilds_standby(client, monkeypatch):
    # DUAL mode: the Populate-tail maintenance button must force a rebuild_standby apply (rebuild the
    # invisible standby now) instead of a live-tail top-up, and report the STANDBY deck's vids.
    tc, ctx, bridge = client
    pool = [{"key": f"k{i}", "video_id": f"v{i}", "artist": chr(64 + i), "title": "t", "url": "u"}
            for i in range(1, 7)]
    monkeypatch.setattr(radio_mod, "pick_next", _pool_pick_next(pool))
    ytms = iter(["PLA", "PLB"])
    monkeypatch.setattr(executor, "create_generated_playlist",
                        lambda *a, **k: {"new_ytm": next(ytms), "pid": 1})
    assert tc.post("/radio/start").json() == {"ok": True}
    assert ctx.radio.dual_deck is False   # confirmation has not landed yet (see the dual-start tests)
    # Simulate the extension's deck-ready confirmation directly (same as
    # tests/test_radio_deck_bridge.py's _dual_session helper): the client fixture's bridge is a
    # _FakeBridge with no connect()/disconnect(), so it cannot drive the real bridge_ws WS handshake.
    ctx.radio.dual_deck = True

    r = tc.post("/radio/populate").json()

    assert r["ok"] is True and "standby" in r
    standby_label = "B" if ctx.radio.live_label == "A" else "A"
    assert r["standby"] == [q["video_id"] for q in ctx.radio.decks[standby_label]["queue"]]
    assert "queue" not in r   # dual response shape, not the v2 one


def test_populate_dual_rebuild_standby_none_is_still_ok_and_unchanged(client, monkeypatch):
    # DUAL mode, rebuild_standby -> None (e.g. catalog exhausted, fail-open "keep prior queue" path,
    # see radio.rebuild_standby): the route must still report ok:True/changed:False with the STANDBY
    # deck's (unchanged) queue, never an error -- and the None plan must be a true no-op through
    # _deck_reconcile_navigate (no reconcile, no deck-navigate/boundary send).
    tc, ctx, bridge = client
    pool = [{"key": f"k{i}", "video_id": f"v{i}", "artist": chr(64 + i), "title": "t", "url": "u"}
            for i in range(1, 7)]
    monkeypatch.setattr(radio_mod, "pick_next", _pool_pick_next(pool))
    ytms = iter(["PLA", "PLB"])
    monkeypatch.setattr(executor, "create_generated_playlist",
                        lambda *a, **k: {"new_ytm": next(ytms), "pid": 1})
    assert tc.post("/radio/start").json() == {"ok": True}
    assert ctx.radio.dual_deck is False   # confirmation has not landed yet (see the dual-start tests)
    # Simulate the extension's deck-ready confirmation directly (same as
    # tests/test_radio_deck_bridge.py's _dual_session helper): the client fixture's bridge is a
    # _FakeBridge with no connect()/disconnect(), so it cannot drive the real bridge_ws WS handshake.
    ctx.radio.dual_deck = True
    standby_label = "B" if ctx.radio.live_label == "A" else "A"
    standby_before = [q["video_id"] for q in ctx.radio.decks[standby_label]["queue"]]
    bridge.sent.clear()
    reconciled = []
    monkeypatch.setattr(executor, "_reconcile", lambda *a, **k: (reconciled.append(a), (0, 0, [], []))[1])
    monkeypatch.setattr(radio_mod, "rebuild_standby", lambda st, se, now: None)

    r = tc.post("/radio/populate").json()

    assert r == {"ok": True, "standby": standby_before, "changed": False}
    assert not reconciled, "a None plan must never reach executor._reconcile"
    assert not any(f["type"] in ("deck-navigate", "deck-boundary-config") for f in bridge.sent)


# --- Visibility wave Part 2: /bridge/status mode transparency + "Up next" (radio_upcoming) ---

def test_bridge_status_carries_radio_dual_and_upcoming_v2(client):
    tc, ctx, bridge = client
    ctx.radio.active = True
    ctx.radio.dual_deck = False
    ctx.radio.queue = [
        {"key": "k1", "video_id": "v1", "artist": "A1", "title": "Now Playing", "url": "u"},
        {"key": "k2", "video_id": "v2", "artist": "A2", "title": "Next One", "url": "u"},
        {"key": "k3", "video_id": "v3", "artist": "A3", "title": "Then This", "url": "u"},
    ]
    ctx.radio.pos = 0   # "Now Playing" is the current track; upcoming excludes it

    r = tc.get("/bridge/status").json()

    assert r["radio_dual"] is False
    assert r["radio_fallback_reason"] is None
    assert r["radio_upcoming"] == [{"title": "Next One", "artist": "A2"},
                                   {"title": "Then This", "artist": "A3"}]


def test_bridge_status_carries_radio_dual_and_upcoming_dual(client):
    tc, ctx, bridge = client
    ctx.radio.active = True
    ctx.radio.dual_deck = True
    ctx.radio.live_label = "A"
    ctx.radio.decks["A"]["queue"] = [
        {"key": "ka1", "video_id": "vA1", "artist": "artA1", "title": "Now", "url": "u"},
        {"key": "ka2", "video_id": "vA2", "artist": "artA2", "title": "LiveNext", "url": "u"},
    ]
    ctx.radio.pos = 0   # index into the LIVE deck's own queue (see radio_mod.upcoming_picks)
    ctx.radio.decks["B"]["queue"] = [
        {"key": "kb1", "video_id": "vB1", "artist": "artB1", "title": "StandbyFirst", "url": "u"},
    ]

    r = tc.get("/bridge/status").json()

    assert r["radio_dual"] is True
    # live deck's remaining queue (after pos), THEN the standby deck's full queue.
    assert r["radio_upcoming"] == [{"title": "LiveNext", "artist": "artA2"},
                                   {"title": "StandbyFirst", "artist": "artB1"}]


def test_bridge_status_radio_upcoming_empty_when_inactive(client):
    tc, ctx, bridge = client
    assert ctx.radio.active is False
    r = tc.get("/bridge/status").json()
    assert r["radio_upcoming"] == []
    assert r["radio_dual"] is False
    assert r["radio_fallback_reason"] is None


def test_stop_clears(client):
    tc, ctx, bridge = client
    tc.post("/radio/start")
    assert tc.post("/radio/stop").json() == {"ok": True}
    assert ctx.radio.active is False
    assert ctx.store.get_setting("radio_active") == "0"
    assert tc.get("/bridge/status").json()["radio"] is False


def test_stop_sends_clearing_prime_frame(client):
    # #93 defect 2: a stale primedUrl left in the extension after /radio/stop would let the next
    # organic track end hijack the tab back into the stopped radio. Stop must best-effort send a
    # clearing radio-prime frame (null url/videoId) so the extension drops its stored prime too.
    tc, ctx, bridge = client
    tc.post("/radio/start")
    bridge.sent.clear()
    assert tc.post("/radio/stop").json() == {"ok": True}
    clears = [f for f in bridge.sent if f["type"] == "radio-prime"]
    assert clears == [{"type": "radio-prime", "url": None, "videoId": None}]


def test_stop_clears_waiting(client):
    # Waiting-state net: /radio/stop must clear a pending waiting flag along with everything else
    # reset() clears, so a stale "click to start" prompt never survives a stop.
    tc, ctx, bridge = client
    tc.post("/radio/start")
    ctx.radio.waiting = True
    assert tc.post("/radio/stop").json() == {"ok": True}
    assert ctx.radio.waiting is False
    assert tc.get("/bridge/status").json()["radio_waiting"] is False


def test_stop_sends_deck_stop(client):
    tc, ctx, bridge = client
    tc.post("/radio/start")
    assert tc.post("/radio/stop").json() == {"ok": True}
    assert any(f["type"] == "deck-stop" for f in bridge.sent)
    assert ctx.radio.active is False and ctx.radio.dual_deck is False


def test_stop_clearing_prime_send_failure_does_not_fail_route(client, monkeypatch):
    # Best-effort: the extension may already be gone (send_control raises), and /radio/stop must
    # still report ok.
    tc, ctx, bridge = client
    tc.post("/radio/start")

    def _raise(payload):
        raise RuntimeError("no extension connected")

    monkeypatch.setattr(bridge, "send_control", _raise)
    assert tc.post("/radio/stop").json() == {"ok": True}
    assert ctx.radio.active is False


def test_steer_sets_session_tilt_and_returns_bars(client):
    # #93 Task 9: /radio/steer writes a SESSION-ONLY tilt on ctx.radio, never a standing lean/weight.
    tc, ctx, bridge = client
    r = tc.post("/radio/steer", data={"axis": "genre:Rock", "weight": "1.6"})
    assert r.status_code == 200
    assert ctx.radio.tilts == {"genre:Rock": 1.6}
    assert 'id="radio-customize-bars"' in r.text
    # Never touches the permanent taste model.
    assert ctx.store.get_leans() == {}


def test_steer_clamps_weight_to_genre_range(client):
    from yt_playlist.rec import rec_params
    tc, ctx, bridge = client
    tc.post("/radio/steer", data={"axis": "era:1990", "weight": "99"})
    assert ctx.radio.tilts["era:1990"] == rec_params.GENRE_MAX


def test_steer_ignores_malformed_or_disallowed_axis(client):
    tc, ctx, bridge = client
    assert tc.post("/radio/steer", data={"axis": "", "weight": "1.5"}).status_code == 200
    assert ctx.radio.tilts == {}
    assert tc.post("/radio/steer", data={"axis": "genre:Rock", "weight": "nope"}).status_code == 200
    assert ctx.radio.tilts == {}
    assert tc.post("/radio/steer", data={"axis": "playlist:x", "weight": "1.5"}).status_code == 200
    assert ctx.radio.tilts == {}


def test_steer_noop_when_no_radio_session(client):
    tc, ctx, bridge = client
    ctx.radio = None
    r = tc.post("/radio/steer", data={"axis": "genre:Rock", "weight": "1.5"})
    assert r.status_code == 200   # fail-open: never a 500


def test_steer_reset_clears_tilts(client):
    tc, ctx, bridge = client
    tc.post("/radio/steer", data={"axis": "genre:Rock", "weight": "1.6"})
    assert ctx.radio.tilts
    r = tc.post("/radio/steer/reset")
    assert r.status_code == 200
    assert ctx.radio.tilts == {}
    assert 'id="radio-customize-bars"' in r.text


# --- #93 fix 3: a successful steer/reset in an active DUAL session must submit the same
# standby-rebuild apply-pool job the WS deck-toggled branch submits, so a live tilt tweak reaches the
# next hand-off instead of sitting inert until an unrelated skip/toggle happens to rebuild it. ---

def _capture_pool(monkeypatch, ctx):
    # `_radio_apply_pool` is a ThreadPoolExecutor built as a LOCAL inside bridge_route.build(ctx), so
    # it is otherwise unreachable from a test. Mirrors tests/test_radio_deck_bridge.py's `_build`
    # helper: monkeypatch ThreadPoolExecutor.__init__ for the duration of the build() call only, and
    # grab the instance the route created (identified by its thread_name_prefix) so the test can
    # submit a synchronizing sentinel job onto the SAME single-worker FIFO pool the route uses.
    from concurrent.futures import ThreadPoolExecutor
    captured = {}
    original_init = ThreadPoolExecutor.__init__

    def capturing_init(self, *a, **k):
        original_init(self, *a, **k)
        if k.get("thread_name_prefix") == "radio-apply":
            captured["pool"] = self
    monkeypatch.setattr(ThreadPoolExecutor, "__init__", capturing_init)
    try:
        router = bridge_route.build(ctx)
    finally:
        monkeypatch.setattr(ThreadPoolExecutor, "__init__", original_init)
    return router, captured["pool"]


def _drain(pool):
    # The pool is single-worker FIFO: a sentinel submitted now only finishes once every job queued
    # ahead of it (e.g. the steer route's fire-and-forget rebuild submission) has already run.
    pool.submit(lambda: None).result(timeout=5)


@pytest.fixture
def dual_client(monkeypatch):
    store = Store(":memory:"); store.init_schema()
    bridge = _FakeBridge()
    radio = RadioSession()
    radio.active = True
    radio.dual_deck = True
    radio.live_label = "A"
    radio.decks["A"]["queue"] = [{"key": "kA1", "video_id": "vA1", "artist": "artA", "title": "A1", "url": "u"}]
    radio.decks["B"]["queue"] = [{"key": "kB1", "video_id": "vB1", "artist": "artB", "title": "B1", "url": "u"}]
    radio.decks["B"]["boundary_vid"] = "vB1"
    radio.decks["B"]["playlist_ytm"] = "PLB"
    ctx = _Ctx(store, bridge, radio, _FakeClient())
    router, pool = _capture_pool(monkeypatch, ctx)
    monkeypatch.setattr(executor, "_reconcile", lambda *a, **k: (0, 0, [], []))
    app = FastAPI(); app.include_router(router)
    return TestClient(app), ctx, bridge, pool


def _stub_rebuild_to_vnew(monkeypatch, calls=None):
    def _rebuild(st, se, now):
        if calls is not None:
            calls.append(se)
        return {"playlist_key": "B", "vids": ["vNEW"], "first": {"video_id": "vNEW"},
                "boundary": "vNEW", "epoch": se.epoch}
    monkeypatch.setattr(radio_mod, "rebuild_standby", _rebuild)


def test_steer_active_dual_submits_standby_rebuild_and_changes_standby(dual_client, monkeypatch):
    tc, ctx, bridge, pool = dual_client
    calls = []
    _stub_rebuild_to_vnew(monkeypatch, calls)

    r = tc.post("/radio/steer", data={"axis": "genre:Rock", "weight": "1.6"})

    assert r.status_code == 200
    assert ctx.radio.tilts == {"genre:Rock": 1.6}
    _drain(pool)
    assert len(calls) == 1 and calls[0] is ctx.radio         # the rebuild job actually ran
    assert ctx.radio.decks["B"]["applied_vids"] == ["vNEW"]  # standby queue reacted to the new tilt
    nav = next(f for f in bridge.sent if f["type"] == "deck-navigate")
    assert "vNEW" in nav["url"] and nav["role"] == "standby"


def test_steer_reset_active_dual_submits_standby_rebuild(dual_client, monkeypatch):
    tc, ctx, bridge, pool = dual_client
    calls = []
    _stub_rebuild_to_vnew(monkeypatch, calls)

    r = tc.post("/radio/steer/reset")

    assert r.status_code == 200
    _drain(pool)
    assert len(calls) == 1 and calls[0] is ctx.radio
    assert ctx.radio.decks["B"]["applied_vids"] == ["vNEW"]


def test_steer_inactive_session_submits_no_rebuild(monkeypatch):
    store = Store(":memory:"); store.init_schema()
    bridge = _FakeBridge()
    radio = RadioSession()   # active defaults False
    ctx = _Ctx(store, bridge, radio, _FakeClient())
    router, pool = _capture_pool(monkeypatch, ctx)
    app = FastAPI(); app.include_router(router)
    tc = TestClient(app)
    calls = []
    _stub_rebuild_to_vnew(monkeypatch, calls)

    r = tc.post("/radio/steer", data={"axis": "genre:Rock", "weight": "1.6"})

    assert r.status_code == 200
    _drain(pool)
    assert calls == []


def _stub_force_topup_to_vnew(monkeypatch, calls=None):
    def _topup(st, se, now):
        if calls is not None:
            calls.append(se)
        return {"desired_vids": ["vNEW"], "prime": None}
    monkeypatch.setattr(radio_mod, "force_topup", _topup)


def _v2_steer_client(monkeypatch):
    store = Store(":memory:"); store.init_schema()
    bridge = _FakeBridge()
    radio = RadioSession()
    radio.active = True
    radio.dual_deck = False
    radio.queue = [{"key": "k1", "video_id": "v1", "artist": "A", "title": "t1", "url": "u"}]
    radio.pos = 0
    radio.playlist_ytm = "PLRADIO"
    ctx = _Ctx(store, bridge, radio, _FakeClient())
    router, pool = _capture_pool(monkeypatch, ctx)
    monkeypatch.setattr(executor, "_reconcile", lambda *a, **k: (0, 0, [], []))
    app = FastAPI(); app.include_router(router)
    return TestClient(app), ctx, bridge, pool


def test_steer_active_v2_submits_tail_refresh_and_changes_queue(monkeypatch):
    # Visibility wave Part 3: v2 single-tab mode is no longer left alone -- a tilt write now schedules
    # the same tail-refresh /radio/populate's v2 body performs (force_topup + apply), so "Up next"
    # (/bridge/status's radio_upcoming) reacts to a slider tweak within a poll or two, matching what
    # the dual branch already did. Superseded #93-fix-3-era test asserted the opposite (no rebuild);
    # this is the deliberate behavior change the brief calls for.
    tc, ctx, bridge, pool = _v2_steer_client(monkeypatch)
    calls = []
    _stub_force_topup_to_vnew(monkeypatch, calls)

    r = tc.post("/radio/steer", data={"axis": "genre:Rock", "weight": "1.6"})

    assert r.status_code == 200
    assert ctx.radio.tilts == {"genre:Rock": 1.6}
    _drain(pool)
    assert len(calls) == 1 and calls[0] is ctx.radio          # the tail-refresh job actually ran
    assert ctx.radio.applied_vids == ["vNEW"]                 # v2 queue reacted to the new tilt


def test_steer_reset_active_v2_submits_tail_refresh(monkeypatch):
    tc, ctx, bridge, pool = _v2_steer_client(monkeypatch)
    calls = []
    _stub_force_topup_to_vnew(monkeypatch, calls)

    r = tc.post("/radio/steer/reset")

    assert r.status_code == 200
    _drain(pool)
    assert len(calls) == 1 and calls[0] is ctx.radio
    assert ctx.radio.applied_vids == ["vNEW"]
