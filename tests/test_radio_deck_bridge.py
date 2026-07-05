"""T7g: bridge deck apply + inbound deck frames (deck-ready, deck-toggled, S2).

Mirrors tests/test_radio_bridge_wiring.py's real-WS `_FramePump` style: the deck helpers
(`_deck_reconcile_navigate`, `_deck_rebuild_standby_apply`) are closures inside `build()`, never
exposed directly, so every path here is driven over the actual bridge WebSocket + the fire-and-
forget apply pool, exactly as production traffic would drive it. `POST /radio/populate` is used as
a synchronization barrier: the apply pool has exactly one worker (FIFO), and populate's route
`await`s its own pool submission, so by the time that HTTP call returns, every apply submitted
before it (e.g. by an earlier WS frame) has already finished running.
"""
import queue
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yt_playlist.core.bridge import Bridge
from yt_playlist.core.store import Store
from yt_playlist.library import executor
from yt_playlist.rec import radio as radio_mod, rec_params
from yt_playlist.rec.radio import RadioSession
from yt_playlist.web.routes import bridge as bridge_mod
from yt_playlist.web.routes.bridge import build as build_bridge_route, EXTENSION_ORIGIN


class _FakeClient:
    pass


class _FramePump:
    """See tests/test_radio_bridge_wiring.py: pumps ws.receive_json() off a single background
    thread so every wait has a real bounded deadline instead of hanging the test run."""

    def __init__(self, ws):
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, args=(ws,), daemon=True).start()

    def _pump(self, ws):
        try:
            while True:
                self._q.put(("frame", ws.receive_json()))
        except Exception as e:  # noqa: BLE001 - socket closed under us; surface it, don't raise here
            self._q.put(("error", e))

    def wait_for(self, pred, tries=20, per_call_timeout=5):
        for _ in range(tries):
            try:
                kind, val = self._q.get(timeout=per_call_timeout)
            except queue.Empty:
                raise AssertionError(
                    f"timed out after {per_call_timeout}s waiting for a matching bridge frame")
            if kind == "error":
                raise AssertionError(f"websocket closed while waiting for a frame: {val!r}")
            if pred(val):
                return val
        raise AssertionError(f"expected frame not received within {tries} bounded reads")

    def drain(self):
        """Non-blocking: every frame already queued right now, in order. Used to assert an ABSENCE
        (e.g. no deck-navigate went out) after a barrier (POST /radio/populate) guarantees the apply
        pool job in question has already finished running."""
        out = []
        while True:
            try:
                kind, val = self._q.get_nowait()
            except queue.Empty:
                return out
            if kind == "frame":
                out.append(val)


def _build(store, radio, now=1_000_000.0):
    bridge_obj = Bridge()
    ctx = type("C", (), {
        "bridge": bridge_obj, "store": store, "radio": radio,
        "now_fn": staticmethod(lambda: now),
        "client_provider": staticmethod(lambda: {1: _FakeClient()}),
    })()
    app = FastAPI()
    # Capture the apply pool by monkey-patching ThreadPoolExecutor to store instances on bridge_mod.
    from concurrent.futures import ThreadPoolExecutor
    original_init = ThreadPoolExecutor.__init__
    def capturing_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if kwargs.get("thread_name_prefix") == "radio-apply":
            bridge_mod._radio_apply_pool = self
    ThreadPoolExecutor.__init__ = capturing_init
    try:
        app.include_router(build_bridge_route(ctx))
    finally:
        ThreadPoolExecutor.__init__ = original_init
    return TestClient(app), bridge_obj


def _dual_session(store):
    # PIN radio_deck_size (never lean on the default): nothing here actually re-picks (on_play,
    # rebuild_standby and toggle_decks are exercised directly / monkeypatched), but pinning keeps
    # this fixture honest against a future drift of the default.
    rec_params.set_param(store, "radio_deck_size", 3)
    s = RadioSession()
    s.active = True
    s.dual_deck = True
    s.live_label = "A"
    return s


def _barrier(_client, cond=None):
    # Drains the single-worker FIFO apply pool directly: no route call, zero side effects.
    # `_client` is unused -- kept only so every call site has the same shape; harmless since it's
    # passed positionally everywhere.
    #
    # `ws.send_json()` returns as soon as the frame is enqueued, before the server's portal thread has
    # necessarily run the WS handler far enough to submit its apply-pool job, so a caller needs to wait
    # for that submission before this barrier's own sentinel can mean anything.
    if cond is not None:
        # Positive expectation: poll a bounded number of times for the real work's own side effect to
        # appear, THEN submit the sentinel -- since the pool is single-worker FIFO, the sentinel
        # completing guarantees every job queued before it (the one `cond` just detected) has also run
        # to completion by the time this returns.
        deadline = time.monotonic() + 2.0
        while not cond():
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "timed out after 2s waiting for expected apply-pool work to appear")
            time.sleep(0.005)
    else:
        # Negative expectation (the test asserts NO work appears): there is no true condition to poll
        # for, so this can only bet on a short fixed delay before draining the pool.
        time.sleep(0.01)
    bridge_mod._radio_apply_pool.submit(lambda: None).result(timeout=5)


def test_stale_epoch_plan_is_dropped_no_reconcile_no_navigate(monkeypatch):
    # A rebuild_standby plan stamped epoch 0, delivered after the session already advanced to
    # epoch 1 (a toggle intervened), must be dropped: no reconcile, no deck-navigate/boundary sent.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.epoch = 1
    radio.decks["B"]["playlist_ytm"] = "PLB"

    monkeypatch.setattr(radio_mod, "on_play", lambda st, se, vid, now: {
        "foreign": False, "at_boundary": True, "standby_dirty": True})
    monkeypatch.setattr(radio_mod, "rebuild_standby", lambda st, se, now: {
        "playlist_key": "B", "vids": ["vSTALE"], "first": {"video_id": "vSTALE"},
        "boundary": "vSTALE", "epoch": 0})
    reconciled = []
    monkeypatch.setattr(executor, "_reconcile", lambda *a, **k: (reconciled.append(a), (0, 0, [], []))[1])

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        pump = _FramePump(ws)
        ws.send_json({"type": "play", "title": "A", "artist": "Art", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "vSTALE"})
        # Negative expectation (no reconcile, no navigate/boundary): nothing here ever becomes true to
        # poll for, so this can only fall back to _barrier's fixed sleep.
        _barrier(client)   # the apply-pool job (stale plan) has now run to completion
        frames = pump.drain()
        # Checked before the socket closes: disconnect resets the session (bridge_ws's finally
        # block) and would otherwise wipe applied_vids back to [] regardless of what happened here.
        assert radio.decks["B"]["applied_vids"] == []   # never touched

    assert not reconciled, "a stale (pre-toggle) plan must never reach executor._reconcile"
    assert not any(f.get("type") in ("deck-navigate", "deck-boundary-config") for f in frames)


def test_fresh_rebuild_plan_reconciles_and_sends_navigate_and_boundary(monkeypatch):
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.epoch = 0
    radio.decks["B"]["playlist_ytm"] = "PLB"

    monkeypatch.setattr(radio_mod, "on_play", lambda st, se, vid, now: {
        "foreign": False, "at_boundary": True, "standby_dirty": True})
    monkeypatch.setattr(radio_mod, "rebuild_standby", lambda st, se, now: {
        "playlist_key": "B", "vids": ["v10", "v11"], "first": {"video_id": "v10"},
        "boundary": "v11", "epoch": 0})
    reconciled = []
    monkeypatch.setattr(
        executor, "_reconcile",
        lambda client, ytm, vids: (reconciled.append((ytm, list(vids))), (0, 0, [], []))[1])

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        pump = _FramePump(ws)
        ws.send_json({"type": "play", "title": "A", "artist": "Art", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "v11"})
        nav = pump.wait_for(lambda f: f.get("type") == "deck-navigate")
        boundary = pump.wait_for(lambda f: f.get("type") == "deck-boundary-config")

        # Check session state BEFORE the socket closes: disconnect resets the session (bridge_ws's
        # finally block), which would otherwise clear applied_vids/decks back to their reset shape.
        assert reconciled == [("PLB", ["v10", "v11"])]
        assert radio.decks["B"]["applied_vids"] == ["v10", "v11"]
    assert "v10" in nav["url"] and "list=PLB" in nav["url"]
    assert boundary["videoId"] == "v11"
    assert boundary["role"] == "standby"   # label "B" != live_label "A"
    assert boundary["epoch"] == 0          # DEFECT 2: boundary-config now carries the epoch echo


def test_deck_toggled_correctly_echoed_epoch_runs_toggle_and_submits_standby_rebuild(monkeypatch):
    # (c) A deck-toggled that echoes the CURRENT session epoch is the genuine, first confirmation of
    # a swap: it must still run toggle_decks and submit the new standby's rebuild (DEFECT 2 fix must
    # not break the happy path).
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.epoch = 0

    calls = []
    monkeypatch.setattr(radio_mod, "rebuild_standby",
                        lambda st, se, now: calls.append((se, now)) or None)

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-toggled", "epoch": 0})   # correctly-echoed: matches session.epoch
        # Poll for the rebuild-apply job's own recorded call, then drain the pool.
        _barrier(client, cond=lambda: len(calls) >= 1)

        # Check state BEFORE the socket closes: disconnect resets the session (see the finally
        # block in bridge_ws), which would otherwise flip live_label/epoch straight back.
        assert radio.live_label == "B"          # toggle_decks actually ran
        assert radio.epoch == 1
        assert len(calls) == 1 and calls[0][0] is radio   # rebuild_standby invoked for the new standby


def test_deck_toggled_stale_epoch_echo_is_ignored(monkeypatch):
    # (b) A duplicate deck-toggled delivery echoes the PRE-toggle epoch (the epoch the first, real
    # confirmation already advanced past). It must not flip live_label back or call toggle_decks again.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.epoch = 5   # simulate: the real toggle already happened and bumped the epoch to 5

    calls = []
    monkeypatch.setattr(radio_mod, "toggle_decks", lambda se: calls.append(se))
    monkeypatch.setattr(radio_mod, "rebuild_standby", lambda st, se, now: None)

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-toggled", "epoch": 4})   # stale/duplicate echo
        # Negative expectation (toggle_decks must never run): nothing here becomes true to poll for.
        _barrier(client)
        assert radio.live_label == "A"    # unchanged
        assert radio.epoch == 5           # unchanged
    assert not calls, "a stale epoch echo must never call toggle_decks"


def test_deck_toggled_untagged_is_ignored(monkeypatch):
    # Backward-safety leg of DEFECT 2: a deck-toggled with no epoch key at all (pre-contract frame)
    # is dropped, not treated as trusted. Safe to be strict: the extension side is unbuilt.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.epoch = 0

    calls = []
    monkeypatch.setattr(radio_mod, "toggle_decks", lambda se: calls.append(se))
    monkeypatch.setattr(radio_mod, "rebuild_standby", lambda st, se, now: None)

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-toggled"})   # no epoch field at all
        # Negative expectation (toggle_decks must never run): nothing here becomes true to poll for.
        _barrier(client)
        assert radio.live_label == "A"
        assert radio.epoch == 0
    assert not calls, "an untagged deck-toggled must never call toggle_decks"


def test_toggle_during_reconcile_drops_navigate_and_boundary_sends(monkeypatch):
    # (a) DEFECT 1 TOCTOU regression: a deck-toggled that completes WHILE executor._reconcile is in
    # flight (its network latency IS the race window) must not have that plan's deck-navigate /
    # deck-boundary-config sent afterward -- by then the reconciled deck is the new LIVE deck, and
    # sending with a role baked at plan time would clobber live playback with the wrong role.
    # Simulate the race by making the reconcile stub itself trigger the toggle synchronously (network
    # latency compressed to zero; the ordering hazard is the same one production hits).
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.epoch = 0
    radio.decks["B"]["playlist_ytm"] = "PLB"

    monkeypatch.setattr(radio_mod, "on_play", lambda st, se, vid, now: {
        "foreign": False, "at_boundary": True, "standby_dirty": True})
    monkeypatch.setattr(radio_mod, "rebuild_standby", lambda st, se, now: {
        "playlist_key": "B", "vids": ["v20", "v21"], "first": {"video_id": "v20"},
        "boundary": "v21", "epoch": 0})

    def _reconcile_triggers_toggle(client, ytm, vids):
        radio_mod.toggle_decks(radio)     # a confirmed swap lands mid-reconcile
        return (0, 0, [], [])
    monkeypatch.setattr(executor, "_reconcile", _reconcile_triggers_toggle)

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        pump = _FramePump(ws)
        ws.send_json({"type": "play", "title": "A", "artist": "Art", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "v21"})
        # Poll for the reconcile's own recorded side effect (set inside the mid-reconcile toggle's
        # stub, before the sends this test asserts are dropped), then drain the pool.
        _barrier(client, cond=lambda: radio.decks["B"]["applied_vids"] == ["v20", "v21"])
        frames = pump.drain()
        # The reconcile itself still landed (deck B's applied_vids updated) -- only the sends dropped.
        assert radio.decks["B"]["applied_vids"] == ["v20", "v21"]
        assert radio.epoch == 1   # the toggle really happened, mid-reconcile
        assert radio.live_label == "B"
    assert not any(f.get("type") in ("deck-navigate", "deck-boundary-config") for f in frames)


def test_deck_ready_sets_dual_deck_flag():
    # Matching-gen path: the echoed "gen" (0, RadioSession's fresh default) equals session.start_gen,
    # so the frame is accepted and dual_deck flips.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.dual_deck = False

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-ready", "fallback": False, "gen": radio.start_gen})
        _barrier(client, cond=lambda: radio.dual_deck is True)
        assert radio.dual_deck is True   # checked before disconnect resets the session


def test_deck_ready_fallback_clears_dual_deck_flag():
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.dual_deck = True

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-ready", "fallback": True, "gen": radio.start_gen})
        _barrier(client, cond=lambda: radio.dual_deck is False)
        assert radio.dual_deck is False   # checked before disconnect resets the session anyway


def test_deck_ready_fallback_stores_reason_and_status_exposes_it():
    # Fallback diagnostics (visibility wave, Part 1): a gen-matched fallback deck-ready stores the
    # extension's reason and /bridge/status surfaces it. `radio.active = False` here isolates this
    # assertion from H3's fallback-START path (test_deck_ready_fallback_true_starts_v2_session_on_pool
    # etc. below), which requires radio.active and would otherwise reset() the session (clearing
    # fallback_reason right back to None) before this test could observe it.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.dual_deck = True
    radio.active = False

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-ready", "fallback": True, "gen": radio.start_gen,
                      "reason": "deck-play not delivered to live tab"})
        _barrier(client, cond=lambda: radio.fallback_reason == "deck-play not delivered to live tab")
        assert radio.fallback_reason == "deck-play not delivered to live tab"
        assert radio.dual_deck is False
        status = client.get("/bridge/status").json()   # read before disconnect resets the session
    assert status["radio_fallback_reason"] == "deck-play not delivered to live tab"
    assert status["radio_dual"] is False


def test_deck_ready_confirmed_dual_clears_prior_fallback_reason():
    # A confirmed (non-fallback) deck-ready means dual is actually working now, so any stale reason
    # from an earlier fallen-back attempt must not keep haunting /bridge/status.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.dual_deck = False
    radio.fallback_reason = "previous failure"

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-ready", "fallback": False, "gen": radio.start_gen})
        _barrier(client, cond=lambda: radio.dual_deck is True)
        assert radio.fallback_reason is None


def test_deck_ready_matching_gen_flips_dual_deck_after_later_attempt(monkeypatch):
    # T7i carried fix (T7h review): a NON-zero start_gen (simulating a session that has already been
    # through one or more /radio/start attempts) still flips dual_deck when the echoed gen matches
    # exactly, so the fix does not just happen to work at the RadioSession default of 0.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.dual_deck = False
    radio.start_gen = 7

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-ready", "fallback": False, "gen": 7})
        _barrier(client, cond=lambda: radio.dual_deck is True)
        assert radio.dual_deck is True


def test_deck_ready_stale_gen_is_ignored(monkeypatch):
    # A deck-ready echoing an OLDER generation (an earlier /radio/start attempt's stamp, delayed in
    # flight) must not flip a later attempt's dual_deck.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.dual_deck = False
    radio.start_gen = 3   # a later attempt has already bumped this past the stale frame's stamp

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-ready", "fallback": False, "gen": 2})   # stale echo
        # Negative expectation (dual_deck must stay False, its initial value): nothing here becomes
        # true to poll for.
        _barrier(client)
        assert radio.dual_deck is False   # ignored: gen mismatch


def test_deck_ready_absent_gen_is_ignored(monkeypatch):
    # A deck-ready with no "gen" key at all (pre-contract frame) must be dropped, not trusted, mirroring
    # deck-toggled's untagged-frame guard (DEFECT 2, T7g).
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.dual_deck = False
    radio.start_gen = 1

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-ready", "fallback": False})   # no "gen" field
        # Negative expectation (dual_deck must stay False, its initial value): nothing here becomes
        # true to poll for.
        _barrier(client)
        assert radio.dual_deck is False   # ignored: no echo to trust


def test_foreign_gated_true_fires_deck_toggle_control_send(monkeypatch):
    # S2 fail-safe: on_play's `foreign` is trusted as-is (already gated at the boundary by radio.py,
    # T7d/T7e); the bridge must fire deck-toggle when it is True AND the frame came from the LIVE
    # deck tab (M4, final review) -- the only tab whose foreign play can mean "YTM autoplay leaked
    # past our boundary".
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.epoch = 0

    monkeypatch.setattr(radio_mod, "on_play", lambda st, se, vid, now: {
        "foreign": True, "at_boundary": False, "standby_dirty": False})

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        pump = _FramePump(ws)
        ws.send_json({"type": "play", "title": "A", "artist": "Art", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "vFOREIGN", "deck": "live"})
        frame = pump.wait_for(lambda f: f.get("type") == "deck-toggle")
    assert frame == {"type": "deck-toggle", "epoch": 0}   # DEFECT 2: epoch echo for dedup


def test_foreign_from_non_live_deck_never_fires_deck_toggle(monkeypatch):
    # M4 (final review): the S2 fail-safe must be gated on the frame coming from the LIVE deck tab.
    # A play frame tagged "unknown" (the user's own YTM tab, outside the radio window) while the
    # session sits at its boundary would otherwise fire a spurious deck swap in the radio window.
    # Determinism: a third, live-tagged NON-foreign frame is used as a sentinel -- its on_play plan
    # submits a rebuild to the FIFO apply pool, so once that job is observed and the pool drained,
    # both earlier frames have been fully handled and any deck-toggle they'd have sent is queued.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.epoch = 0

    def _on_play(st, se, vid, now):
        if vid == "vFOREIGN":
            return {"foreign": True, "at_boundary": False, "standby_dirty": False}
        return {"foreign": False, "at_boundary": True, "standby_dirty": True}
    monkeypatch.setattr(radio_mod, "on_play", _on_play)
    rebuilds = []
    monkeypatch.setattr(radio_mod, "rebuild_standby",
                        lambda st, se, now: rebuilds.append(1) or None)

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        pump = _FramePump(ws)
        base = {"type": "play", "title": "A", "artist": "Art", "thumbnail": "",
                "likeStatus": "INDIFFERENT"}
        ws.send_json(dict(base, videoId="vFOREIGN"))                     # untagged -> "unknown"
        ws.send_json(dict(base, videoId="vFOREIGN", deck="unknown"))     # explicit unknown
        ws.send_json(dict(base, videoId="vSENTINEL", deck="live"))       # sentinel (pool barrier)
        _barrier(client, cond=lambda: len(rebuilds) >= 1)
        frames = pump.drain()
    assert not any(f.get("type") == "deck-toggle" for f in frames), \
        "a foreign play from a non-live tab must never toggle the decks"


def test_deck_ready_fallback_true_starts_v2_session_on_pool(monkeypatch):
    # H3 (final review): a gen-matching deck-ready {fallback:true} while the session is active must
    # actually START the v2 single-tab session -- seed session.queue, resolve the v2 playlist,
    # navigate + prime -- via the single-worker apply pool, not just flip dual_deck. The extension
    # tears the radio window down on every degradation path, so flag-only fallback is silence with
    # the UI saying playing.
    store = Store(":memory:"); store.init_schema()
    rec_params.set_param(store, "radio_seed_depth", 2)
    radio = _dual_session(store)
    radio.dual_deck = False   # dual never confirmed; the extension reports fallback instead

    picks = iter([{"key": "k1", "video_id": "v1", "artist": "A", "title": "t", "url": "u"},
                  {"key": "k2", "video_id": "v2", "artist": "B", "title": "t", "url": "u"}])
    monkeypatch.setattr(radio_mod, "pick_next", lambda st, se, now: next(picks, None))
    monkeypatch.setattr(executor, "create_generated_playlist",
                        lambda *a, **k: {"new_ytm": "PLRADIO", "pid": 1})
    monkeypatch.setattr(executor, "_reconcile", lambda *a, **k: (0, 0, [], []))

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        pump = _FramePump(ws)
        ws.send_json({"type": "deck-ready", "fallback": True, "gen": radio.start_gen})
        nav = pump.wait_for(lambda f: f.get("type") == "navigate")
        prime = pump.wait_for(lambda f: f.get("type") == "radio-prime")
        _barrier(client, cond=lambda: bool(radio.queue))
        # Checked before the socket closes (disconnect resets the session): the session ends
        # CONSISTENT -- active, single-tab, queue populated, playlist resolved.
        assert radio.active is True and radio.dual_deck is False
        assert [q["video_id"] for q in radio.queue] == ["v1", "v2"]
        assert radio.playlist_ytm == "PLRADIO"
        assert store.get_setting("radio_active") != "0"
    assert "v1" in nav["url"] and "list=PLRADIO" in nav["url"]
    assert prime["videoId"] == "v2" and "list=PLRADIO" in prime["url"]


def test_deck_ready_fallback_true_with_nothing_pickable_stops_honestly(monkeypatch):
    # H3, honest leg: if the v2 fallback start cannot seed anything (or fails), the session must
    # end honestly stopped -- reset + radio_active "0" -- instead of active-but-silent with the UI
    # claiming radio is playing.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.dual_deck = False

    monkeypatch.setattr(radio_mod, "pick_next", lambda st, se, now: None)

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-ready", "fallback": True, "gen": radio.start_gen})
        _barrier(client, cond=lambda: radio.active is False)
        assert radio.active is False
        assert store.get_setting("radio_active") == "0"


def test_deck_waiting_pevent_tagged_standby_still_sets_waiting():
    # ATTRIBUTION SUBTLETY (waiting-state net): deck-play is sent to the about-to-be-promoted tab
    # BEFORE toggleDecks swaps liveTabId/standbyTabId, so a deck-waiting report for that same episode
    # can race the swap and land tagged "standby". It must still be honored (session.waiting = True),
    # handled BEFORE the standby-drop guard, not silently dropped by it.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.waiting = False

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "pevent", "kind": "deck-waiting", "deck": "standby",
                      "detail": {"err": "NotAllowedError"}})
        _barrier(client, cond=lambda: radio.waiting is True)
        assert radio.waiting is True   # checked before disconnect resets the session


def test_deck_waiting_pevent_tagged_live_also_sets_waiting():
    # The other side of the same race: the report can just as easily land tagged "live".
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.waiting = False

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "pevent", "kind": "deck-waiting", "deck": "live",
                      "detail": {"err": "NotAllowedError"}})
        _barrier(client, cond=lambda: radio.waiting is True)
        assert radio.waiting is True


def test_live_play_frame_clears_waiting():
    # A real (non-standby) play frame is the resolution signal for a pending waiting episode.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.waiting = True

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "play", "title": "A", "artist": "Art", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "v1"})
        _barrier(client, cond=lambda: radio.waiting is False)
        assert radio.waiting is False


def test_bridge_status_carries_radio_waiting():
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.active = True
    radio.waiting = True
    client, bridge_obj = _build(store, radio)
    assert client.get("/bridge/status").json()["radio_waiting"] is True
    radio.waiting = False
    assert client.get("/bridge/status").json()["radio_waiting"] is False


def test_deck_ready_fallback_stale_gen_never_submits_v2_start(monkeypatch):
    # H3 guard: a stale-generation fallback echo (an earlier /radio/start attempt's frame) must not
    # kick off a v2 fallback start against the CURRENT session.
    store = Store(":memory:"); store.init_schema()
    radio = _dual_session(store)
    radio.dual_deck = False
    radio.start_gen = 3

    seeded = []
    monkeypatch.setattr(radio_mod, "start_session",
                        lambda st, se, now: seeded.append(1) or None)

    client, bridge_obj = _build(store, radio)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        ws.send_json({"type": "deck-ready", "fallback": True, "gen": 2})   # stale echo
        # Negative expectation: nothing here ever becomes true to poll for.
        _barrier(client)
        assert radio.active is True          # untouched
        assert radio.dual_deck is False      # untouched (gen mismatch dropped the frame entirely)
    assert not seeded, "a stale-gen fallback must never reach start_session"
