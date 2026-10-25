"""Shared browser fixtures registered once for mixed paths and node selectors.

Pytest can collect more than one Directory node for tests/browser in a mixed
selection. Plugin fixtures remain visible to every such node.
"""
import pathlib
import logging
import socket
import threading
import time

import pytest

_BROWSER_DIR = pathlib.Path(__file__).parent / 'browser'


@pytest.fixture(autouse=True)
def restore_server_logging(request):
    """A live test server's dictConfig must not replace later tests' logging policy."""
    if _BROWSER_DIR not in request.node.path.parents:
        yield
        return
    saved = []
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        logger = logging.getLogger(name)
        saved.append((logger, list(logger.handlers), logger.level, logger.propagate, logger.disabled))
    yield
    for logger, handlers, level, propagate, disabled in saved:
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled


def pytest_collection_modifyitems(items):
    """Everything under tests/browser/ is a live-browser test: auto-mark it `browser` so the default
    `-m 'not browser'` (pyproject addopts) deselects it. Browser tests need Playwright's `page` fixture,
    which isn't installed in the default test env. Run them explicitly with `-m browser`.

    The plugin receives the whole session's items, so marking is scoped to this
    directory; ordinary tests in mixed selections keep their existing markers."""
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
    import uvicorn
    from yt_playlist.web.app import create_app
    from tests.conftest import FakeClient

    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.playlists.upsert_playlist(iid, "PLZ", "Old Mix", 3, "h", 0.0)
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
