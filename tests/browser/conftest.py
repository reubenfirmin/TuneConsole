"""Live-server fixture for Playwright tests: a real uvicorn server (Playwright needs
real HTTP + a JS runtime, which TestClient does not provide), seeded with one playlist."""
import pathlib
import socket
import threading
import time

import pytest
import uvicorn

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient

_BROWSER_DIR = pathlib.Path(__file__).parent


def pytest_collection_modifyitems(items):
    """Everything under tests/browser/ is a live-browser test: auto-mark it `browser` so the default
    `-m 'not browser'` (pyproject addopts) deselects it. Browser tests need Playwright's `page` fixture,
    which isn't installed in the default test env. Run them explicitly with `-m browser`.

    NOTE: a subdirectory conftest's collection hook still receives the WHOLE session's items, so we
    must scope to this directory ourselves. Otherwise we'd mark (and deselect) the entire suite."""
    for item in items:
        if _BROWSER_DIR in item.path.parents:
            item.add_marker("browser")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_app(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLZ", "Old Mix", 3, "h", 0.0)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:        # wait until the socket is bound
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
