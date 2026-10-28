import asyncio
import threading
import pytest
from yt_playlist.core.bridge import Bridge, BridgeError
from yt_playlist.core.activity import WorkClosing, cancellable_work
from yt_playlist.integrations.youtube_music import YouTubeMusicIAO


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


def test_sender_failure_finishes_without_waiting_for_timeout_or_replaying(loop):
    bridge = Bridge()
    calls = []
    async def send(frame):
        calls.append(frame)
        raise RuntimeError('private transport detail')
    bridge.connect(send, loop)
    with pytest.raises(BridgeError) as caught:
        bridge.execute('POST', 'https://music.youtube.com/youtubei/v1/playlist/create', {}, timeout=2)
    assert caught.value.outcome == 'uncertain'
    assert 'private' not in str(caught.value)
    assert len(calls) == 1 and not bridge._pending


def test_iao_read_cancellation_releases_pending_and_admission_without_auth_failure(loop):
    bridge = Bridge()
    sent, stop = threading.Event(), threading.Event()
    frames, errors = [], []
    async def send(frame):
        frames.append(frame)
        sent.set()
    bridge.connect(send, loop)
    access = YouTubeMusicIAO(bridge, min_interval=0)
    def read():
        try:
            with cancellable_work(stop.is_set):
                access.request('POST', 'https://music.youtube.com/youtubei/v1/next', {})
        except WorkClosing as error:
            errors.append(error)
    thread = threading.Thread(target=read)
    thread.start()
    try:
        assert sent.wait(1)
        stop.set()
        thread.join(timeout=1)
        assert not thread.is_alive() and len(errors) == 1
        assert len(frames) == 1 and not bridge._pending and access._in_flight == 0
        assert not access.availability().error
    finally:
        stop.set()
        bridge.disconnect()
        thread.join(timeout=1)


def test_iao_dispatched_mutation_drains_before_cancellation_is_reported(loop):
    bridge = Bridge()
    stop = threading.Event()
    frames, completed = [], []
    async def send(frame):
        frames.append(frame)
        stop.set()
        bridge.resolve(frame['id'], 200, '{"status":"STATUS_SUCCEEDED"}')
    bridge.connect(send, loop)
    access = YouTubeMusicIAO(bridge, min_interval=0)
    with pytest.raises(WorkClosing):
        with cancellable_work(stop.is_set):
            response = access.request('POST', 'https://music.youtube.com/youtubei/v1/browse/edit_playlist', {})
            # The domain can publish its write journal before the enclosing
            # cancellation scope exits; the access layer does not replay it.
            completed.append(response.json())
    assert completed == [{'status': 'STATUS_SUCCEEDED'}]
    assert len(frames) == 1 and not bridge._pending and access._in_flight == 0


def test_bridge_timeout_is_uncertain_and_does_not_leave_a_pending_request(loop):
    bridge = Bridge()
    frames = []
    async def send(frame):
        frames.append(frame)  # The extension receives it but no reply returns.
    bridge.connect(send, loop)
    with pytest.raises(BridgeError) as caught:
        bridge.execute('POST', 'https://music.youtube.com/youtubei/v1/playlist/create', {}, timeout=.1)
    assert caught.value.outcome == 'uncertain' and 'timed out' in str(caught.value)
    assert len(frames) == 1 and not bridge._pending
