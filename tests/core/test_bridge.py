import asyncio
import threading
import pytest
from yt_playlist.core.bridge import Bridge, BridgeError


def _run_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    t = threading.Thread(target=_run_loop, args=(lp,), daemon=True)
    t.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)


def test_execute_round_trips_via_sender(loop):
    bridge = Bridge()
    sent = []

    async def send(frame):
        sent.append(frame)
        # Simulate the extension replying on the loop thread.
        bridge.resolve(frame["id"], 200, '{"ok": true}')

    bridge.connect(send, loop)
    status, text = bridge.execute("POST", "https://music.youtube.com/youtubei/v1/browse", {"browseId": "X"})
    assert status == 200
    assert text == '{"ok": true}'
    assert sent[0]["method"] == "POST"
    assert sent[0]["url"].endswith("/browse")
    assert sent[0]["body"] == {"browseId": "X"}


def test_execute_raises_when_not_connected():
    bridge = Bridge()
    with pytest.raises(BridgeError):
        bridge.execute("POST", "https://music.youtube.com/youtubei/v1/browse", {})


def test_disconnect_fails_pending(loop):
    bridge = Bridge()

    async def send(frame):
        pass  # never resolves; we disconnect instead

    bridge.connect(send, loop)

    result = {}

    def call():
        try:
            bridge.execute("POST", "https://music.youtube.com/youtubei/v1/browse", {}, timeout=5)
        except BridgeError as e:
            result["err"] = str(e)

    t = threading.Thread(target=call)
    t.start()
    while not bridge._pending:  # wait until the request is registered
        pass
    bridge.disconnect()
    t.join(timeout=5)
    assert "err" in result


def test_stale_disconnect_does_not_tear_down_current_connection(loop):
    # C2: an older connection's teardown (e.g. its finally block running late) must not nuke a
    # newer connection that has since taken over.
    bridge = Bridge()

    async def send_a(frame):
        pass

    async def send_b(frame):
        bridge.resolve(frame["id"], 200, '{"ok": true}')

    conn_a = bridge.connect(send_a, loop)
    conn_b = bridge.connect(send_b, loop)
    assert conn_a != conn_b

    bridge.disconnect(conn_a)
    assert bridge.connected

    status, text = bridge.execute(
        "POST", "https://music.youtube.com/youtubei/v1/browse", {}, timeout=5)
    assert status == 200
    assert text == '{"ok": true}'

    bridge.disconnect(conn_b)
    assert not bridge.connected
