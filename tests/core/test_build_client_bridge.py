import json
from types import SimpleNamespace
from yt_playlist.core.bridge import Bridge
from yt_playlist.core.identities import build_client


class FakeConn:
    def __init__(self, bridge):
        self.bridge = bridge

    def reply(self, frame):
        # Minimal valid library-playlists browse response shell.
        self.bridge.resolve(frame["id"], 200, json.dumps({"contents": {}}))


def test_build_client_routes_get_library_playlists_through_bridge(monkeypatch):
    bridge = Bridge()
    sent = []

    async def send(frame):
        sent.append(frame)
        bridge.resolve(frame["id"], 200, json.dumps({"contents": {}}))

    import asyncio, threading
    lp = asyncio.new_event_loop()
    threading.Thread(target=lambda: (asyncio.set_event_loop(lp), lp.run_forever()), daemon=True).start()
    bridge.connect(send, lp)

    cfg = SimpleNamespace(brand_account_id=None, label="me")
    client = build_client(cfg, bridge)
    # Any call that hits the network must go through the bridge; assert a frame was sent.
    try:
        client.get_library_playlists(limit=1)
    except Exception:
        pass  # response shell is minimal; we only assert routing here
    assert sent, "no request routed through the bridge"
    assert "/youtubei/v1/" in sent[0]["url"]
