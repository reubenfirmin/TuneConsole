"""A requests.Session whose calls are executed by the browser extension via the Bridge.

ytmusicapi accepts a custom requests_session and funnels every InnerTube call through
session.post()/.get(). We intercept request(), drop the auth-bearing headers/cookies (the extension
supplies fresh auth), ship {url, body} over the bridge, and rebuild a requests.Response so ytmusicapi
parses the reply exactly as if it had hit the network.
"""
import json
from urllib.parse import urlencode

import requests

from yt_playlist.core.bridge import BridgeError


def _unauthorized_response(url: str, message: str) -> requests.Response:
    """Build a synthetic 401 shaped so ytmusicapi's error handling (which does
    response_text.get("error", {}).get("message")) reads the message, routing sync to the
    re-auth path (see library/sync.py:_is_auth_error)."""
    resp = requests.Response()
    resp.status_code = 401
    resp._content = json.dumps({"error": {"message": message}}).encode("utf-8")
    resp.encoding = "utf-8"
    resp.url = url
    resp.reason = "Unauthorized"
    return resp


class BridgeSession(requests.Session):
    def __init__(self, bridge):
        super().__init__()
        self._bridge = bridge

    def request(self, method, url, **kwargs):  # noqa: D401 - override
        params = kwargs.get("params")
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"
        body = kwargs.get("json")  # ytmusicapi passes the InnerTube body as json=...
        # A BridgeError means the extension itself couldn't run the request (no extension connected,
        # the socket dropped, a send/timeout). That is a CONNECTION problem, not a signed-out session,
        # so let it propagate as-is: sync treats it as a soft skip and must NOT flag re-auth on it.
        status, text = self._bridge.execute(method.upper(), url, body)
        if status == 0:
            # The extension ran but the in-page fetch never produced an HTTP status. It returns
            # {status: 0, body: {"error": "..."}} for: genuinely signed-out ("not signed in"), or
            # connection-ish states ("open music.youtube.com", "content script not ready"). Only the
            # first is an auth problem; the rest are connection issues, so raise BridgeError for them.
            message = text
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "error" in parsed:
                    message = parsed["error"]
            except (ValueError, TypeError):
                pass
            if "not signed in" in (message or "").lower():
                return _unauthorized_response(url, message)   # genuine signed-out -> re-auth path
            raise BridgeError(message or "extension could not reach YouTube Music")
        resp = requests.Response()
        resp.status_code = status
        resp._content = text.encode("utf-8")
        resp.encoding = "utf-8"
        resp.url = url
        resp.reason = "OK" if status < 400 else "Error"
        return resp
