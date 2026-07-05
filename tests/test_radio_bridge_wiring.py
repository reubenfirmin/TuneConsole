import inspect
import queue
import threading

from yt_playlist.web.routes import bridge


def test_ws_loop_wires_radio_v2():
    src = inspect.getsource(bridge)
    assert "RADIO_PROVENANCE" not in src                 # TC-RADIO stamp retired
    assert "dispatched_vids" not in src
    assert "radio_mod.on_play" in src                    # play branch drives the queue
    assert "radio_mod.react" in src                      # pevent branch feeds the model
    assert "executor._reconcile" in src                  # playlist mutation reuses the executor
    assert "playlist_watch_url" in src                   # every radio URL carries &list=


# --- live end-to-end: real Bridge, real WS, real store (review follow-up: the source-inspection
# tests above prove wiring exists; this proves the wired path actually works over the socket).
#
# `ws.receive_json()` blocks on an anyio memory stream with no timeout of its own, so a wiring
# regression (e.g. the play branch stops sending radio-prime) would hang this test forever instead
# of failing it. `_FramePump` pumps frames off the socket on ONE background (daemon) thread into a
# `queue.Queue`, so every wait has a real deadline: a regression fails fast with an AssertionError
# instead of hanging the whole test run. A single shared pump (not one thread per wait) matters: two
# threads both calling `ws.receive_json()` would race for the same frames and could steal one meant
# for the other wait. ---

class _FramePump:
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
        """Read frames until `pred(frame)` is true, or fail. Never blocks past
        `tries * per_call_timeout` seconds even if the socket never sends the frame we want."""
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


def test_ws_play_frame_advances_radio_queue_and_reconciles_playlist(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from yt_playlist.core.bridge import Bridge
    from yt_playlist.core.store import Store
    from yt_playlist.library import executor
    from yt_playlist.rec import radio as radio_mod
    from yt_playlist.rec.radio import RadioSession
    from yt_playlist.web.routes.bridge import build as build_bridge_route, EXTENSION_ORIGIN

    store = Store(":memory:")
    store.init_schema()
    store.upsert_identity("main", "cred", None, True)

    class _FakeClient:
        pass

    bridge_obj = Bridge()
    radio = RadioSession()
    ctx = type("C", (), {
        "bridge": bridge_obj, "store": store, "radio": radio,
        "now_fn": staticmethod(lambda: 1_000_000.0),
        "client_provider": staticmethod(lambda: {1: _FakeClient()}),
    })()
    app = FastAPI()
    app.include_router(build_bridge_route(ctx))

    # 6 deterministic picks: 3 seed the session (/radio/start), 3 more top up the tail once on_play
    # advances past the head. Depth is PINNED to 3 here (never lean on the default: it moved once
    # and silently starved this iterator). The mock ignores exclusions (it just hands out the next
    # id), which is fine: this test is about wiring, not the picker's own logic
    # (covered by tests/test_radio_picker.py).
    from yt_playlist.rec import rec_params
    rec_params.set_param(store, "radio_seed_depth", 3)
    picks = iter([
        {"key": "a|Art", "video_id": "va", "artist": "Art", "title": "A",
         "url": "https://music.youtube.com/watch?v=va"},
        {"key": "b|Art2", "video_id": "vb", "artist": "Art2", "title": "B",
         "url": "https://music.youtube.com/watch?v=vb"},
        {"key": "c|Art3", "video_id": "vc", "artist": "Art3", "title": "C",
         "url": "https://music.youtube.com/watch?v=vc"},
        {"key": "d|Art4", "video_id": "vd", "artist": "Art4", "title": "D",
         "url": "https://music.youtube.com/watch?v=vd"},
        {"key": "e|Art5", "video_id": "ve", "artist": "Art5", "title": "E",
         "url": "https://music.youtube.com/watch?v=ve"},
        {"key": "f|Art6", "video_id": "vf", "artist": "Art6", "title": "F",
         "url": "https://music.youtube.com/watch?v=vf"},
    ])
    # This test covers the v2 SINGLE-deck flow end to end. /radio/start now attempts the dual plan
    # first (T7h); with this 6-pick iterator and the default deck size it would legitimately succeed
    # and never emit the navigate frame this test waits for. Force the dual seed empty (dual_deck is
    # provisionally True inside start_dual_session) so the route falls back to the v2 path under test.
    def _pick(st, se, now):
        if se.dual_deck:
            return None
        return next(picks, None)
    monkeypatch.setattr(radio_mod, "pick_next", _pick)
    monkeypatch.setattr(executor, "create_generated_playlist",
                        lambda *a, **k: {"new_ytm": "PLRADIO", "pid": 1})
    reconciled = []
    monkeypatch.setattr(
        executor, "_reconcile",
        lambda client, ytm, vids: (reconciled.append(list(vids)), (0, 0, [], []))[1])

    client = TestClient(app)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        pump = _FramePump(ws)
        assert client.post("/radio/start").json() == {"ok": True}
        # The start navigate + prime frames land first; drain them before the play frame's reaction.
        pump.wait_for(lambda f: f.get("type") == "navigate" and "va" in f["url"])
        pump.wait_for(lambda f: f.get("type") == "radio-prime" and f.get("videoId") == "vb")

        # A play frame for the seeded head track: on_play advances the queue past it, drops+rebuilds
        # the unplayed tail, and _radio_apply reconciles the app playlist + re-primes the new tail.
        ws.send_json({"type": "play", "title": "A", "artist": "Art", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "va"})

        frame = pump.wait_for(lambda f: f.get("type") == "radio-prime" and f.get("videoId") == "vd")
        assert "vd" in frame["url"] and "PLRADIO" in frame["url"]

    assert reconciled, "on_play's queue rebuild must reconcile the app playlist via executor._reconcile"
    assert reconciled[-1] == ["va", "vd", "ve", "vf"]


def test_radio_apply_defers_commit_until_reconcile_succeeds(monkeypatch):
    """#93 review hardening: on_play must not commit `applied_vids` itself; the bridge's
    `_radio_apply` commits it ONLY once `executor._reconcile` returns without raising. A failed
    reconcile (network hiccup, revoked auth, ...) must leave `applied_vids` at its prior value so an
    unchanged `desired_vids` from a later play frame still reads as a real delta and is retried,
    instead of being silently treated as already applied.

    `radio_mod.on_play` is stubbed to hand back the identical plan on every call (isolating the
    bridge's commit-on-success contract from the picker's own determinism, which is covered
    elsewhere), while `executor._reconcile` is stubbed to raise once, then succeed.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from yt_playlist.core.bridge import Bridge
    from yt_playlist.core.store import Store
    from yt_playlist.library import executor
    from yt_playlist.rec import radio as radio_mod
    from yt_playlist.rec.radio import RadioSession
    from yt_playlist.web.routes.bridge import build as build_bridge_route, EXTENSION_ORIGIN

    store = Store(":memory:")
    store.init_schema()
    store.upsert_identity("main", "cred", None, True)

    class _FakeClient:
        pass

    bridge_obj = Bridge()
    radio = RadioSession()
    radio.active = True
    radio.playlist_ytm = "PLRADIO"
    seed_applied = ["va", "vb", "vc"]
    radio.applied_vids = list(seed_applied)          # already-reconciled seed, pre-existing
    ctx = type("C", (), {
        "bridge": bridge_obj, "store": store, "radio": radio,
        "now_fn": staticmethod(lambda: 1_000_000.0),
        "client_provider": staticmethod(lambda: {1: _FakeClient()}),
    })()
    app = FastAPI()
    app.include_router(build_bridge_route(ctx))

    desired = ["va", "vd", "ve", "vf"]
    monkeypatch.setattr(radio_mod, "on_play",
                        lambda st, se, vid, now: {"desired_vids": list(desired),
                                                  "prime": {"video_id": "vd"}})
    calls = {"n": 0}
    reconciled = []

    def _flaky_reconcile(client, ytm, vids):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network hiccup")
        reconciled.append(list(vids))
        return (0, 0, [], [])
    monkeypatch.setattr(executor, "_reconcile", _flaky_reconcile)

    client = TestClient(app)
    with client.websocket_connect("/bridge/ws", headers={"origin": EXTENSION_ORIGIN}) as ws:
        pump = _FramePump(ws)
        # First play frame: on_play hands back `desired`, but the reconcile raises -> the WS loop
        # processes this frame fully (including the failed apply) before the socket reads the next
        # one, so no commit and no radio-prime frame come out of it.
        ws.send_json({"type": "play", "title": "A", "artist": "Art", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "va"})
        # Second play frame: on_play hands back the SAME desired list (the contract: it never
        # committed, so this is still a genuine delta against the still-stale applied_vids). This
        # time the reconcile succeeds and the bridge commits.
        ws.send_json({"type": "play", "title": "A", "artist": "Art", "thumbnail": "",
                      "likeStatus": "INDIFFERENT", "videoId": "va"})
        pump.wait_for(lambda f: f.get("type") == "radio-prime" and f.get("videoId") == "vd")

        # Check the commit state BEFORE the socket closes: disconnect resets the session (see
        # bridge_ws's finally block), which would otherwise clear applied_vids back to [].
        assert calls["n"] == 2                      # the failed attempt was retried, not skipped
        assert reconciled == [desired]              # only the successful call actually reconciled
        assert radio.applied_vids == desired        # committed only once reconcile actually succeeded
