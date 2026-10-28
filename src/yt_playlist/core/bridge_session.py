"""A requests.Session whose calls are executed by the browser extension via the Bridge.

ytmusicapi accepts a custom requests_session and funnels every InnerTube call through
session.post()/.get(). We intercept request(), drop the auth-bearing headers/cookies (the extension
supplies fresh auth), ship {url, body} over the bridge, and rebuild a requests.Response so ytmusicapi
parses the reply exactly as if it had hit the network.
"""
from urllib.parse import urlencode

import requests

from yt_playlist.integrations.youtube_music import for_bridge


class BridgeSession(requests.Session):
    def __init__(self, bridge, *, identity=None, access=None):
        super().__init__()
        self._bridge = bridge
        self._identity = identity
        self._access = access if access is not None else for_bridge(bridge)

    def request(self, method, url, **kwargs):  # noqa: D401 - override
        params = kwargs.get("params")
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"
        body = kwargs.get("json")  # ytmusicapi passes the InnerTube body as json=...
        return self._access.request(method.upper(), url, body, identity=self._identity)
