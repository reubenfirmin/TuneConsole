"""Local-host TestClient factory shared by focused route tests."""
from yt_playlist.web.app import create_app


def _client(store, provider):
    from fastapi.testclient import TestClient
    # base_url is a local host so POSTs pass the cross-origin guard.
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")
