import json
import pytest
from yt_playlist.core.bridge import BridgeError
from yt_playlist.core.bridge_session import BridgeSession


class FakeBridge:
    def __init__(self):
        self.calls = []

    def execute(self, method, url, body, timeout=30.0):
        self.calls.append((method, url, body))
        return 200, json.dumps({"echo": body, "url": url})


class ExtensionErrorBridge:
    """Simulates the extension returning status 0 for not-signed-in / no-tab / blocked / fetch-fail."""

    def execute(self, method, url, body, timeout=30.0):
        return 0, json.dumps({"error": "not signed in"})


class DisconnectedBridge:
    """Simulates bridge.execute raising BridgeError (timeout/disconnected)."""

    def execute(self, method, url, body, timeout=30.0):
        raise BridgeError("disconnected")


class NoTabBridge:
    """Simulates a status-0 error frame that is a CONNECTION issue, not a signed-out session."""

    def execute(self, method, url, body, timeout=30.0):
        return 0, json.dumps({"error": "open music.youtube.com"})


def test_post_routes_body_and_drops_auth():
    bridge = FakeBridge()
    s = BridgeSession(bridge)
    resp = s.post("https://music.youtube.com/youtubei/v1/browse",
                  json={"browseId": "X"},
                  headers={"Authorization": "SAPISIDHASH secret"},
                  cookies={"SAPISID": "secret"})
    assert resp.status_code == 200
    assert resp.json()["echo"] == {"browseId": "X"}
    method, url, body = bridge.calls[0]
    assert method == "POST"
    assert body == {"browseId": "X"}
    # The credential must never reach the bridge.
    assert "secret" not in json.dumps(bridge.calls)


def test_get_encodes_params_into_url():
    bridge = FakeBridge()
    s = BridgeSession(bridge)
    resp = s.get("https://music.youtube.com/youtubei/v1/x", params={"key": "abc", "prettyPrint": "false"})
    assert resp.status_code == 200
    _, url, _ = bridge.calls[0]
    assert "key=abc" in url and "prettyPrint=false" in url


def test_extension_error_signed_out_becomes_synthetic_401():
    # A status-0 frame that says "not signed in" is a GENUINE auth problem: it must route to the
    # re-auth path. ytmusicapi reads response_text.get("error", {}).get("message").
    bridge = ExtensionErrorBridge()
    s = BridgeSession(bridge)
    resp = s.post("https://music.youtube.com/youtubei/v1/browse", json={"browseId": "X"})
    assert resp.status_code == 401
    assert resp.reason == "Unauthorized"
    payload = json.loads(resp.text)
    assert payload["error"]["message"] == "not signed in"


def test_extension_error_no_tab_raises_bridge_error_not_401():
    # A status-0 frame that is a CONNECTION issue ("open music.youtube.com") is NOT signed-out, so it
    # must surface as a BridgeError (sync soft-skips it), never a 401 that would flag re-auth.
    s = BridgeSession(NoTabBridge())
    with pytest.raises(BridgeError):
        s.post("https://music.youtube.com/youtubei/v1/browse", json={"browseId": "X"})


def test_bridge_error_propagates_not_swallowed_as_401():
    # A BridgeError (timeout/disconnected) is a connection problem, not a dead session: it must
    # propagate as-is so sync treats it as a soft skip and does NOT flag re-auth.
    s = BridgeSession(DisconnectedBridge())
    with pytest.raises(BridgeError):
        s.post("https://music.youtube.com/youtubei/v1/browse", json={"browseId": "X"})
