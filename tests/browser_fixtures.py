"""Shared browser fixtures registered once for mixed paths and node selectors.

Pytest can collect more than one Directory node for tests/browser in a mixed
selection. Plugin fixtures remain visible to every such node.
"""
import pathlib
import logging
import os
import socket
import tempfile
from contextlib import contextmanager

import pytest

from tests.live_server import LiveServer

_BROWSER_DIR = pathlib.Path(__file__).parent / 'browser'


@pytest.fixture(scope='session')
def browser_type_launch_args(browser_type_launch_args, stdlib_test_storage):
    """Keep Chromium's singleton socket below the Unix socket path limit.

    The selected fixture volume can have an arbitrarily long path. Chromium
    creates its persistent-profile socket under TMPDIR, even when its profile
    lives elsewhere. Give the browser a short alias to the same owned storage;
    Python temporaries and all browser data still use the selected volume.
    """
    @contextmanager
    def short_path():
        if pathlib.Path('/proc/self/fd').is_dir():
            descriptor = os.open(stdlib_test_storage, os.O_RDONLY | os.O_DIRECTORY)
            try:
                # The parent owns this open descriptor until every browser has
                # stopped. No directory or symlink needs space on /tmp.
                yield pathlib.Path(f'/proc/{os.getpid()}/fd/{descriptor}')
            finally:
                os.close(descriptor)
        else:
            with tempfile.TemporaryDirectory(dir='/tmp', prefix='tuneconsole-browser-workspace-') as alias:
                temporary = pathlib.Path(alias) / 'tmp'
                temporary.symlink_to(stdlib_test_storage, target_is_directory=True)
                yield temporary

    with short_path() as temporary:
        yield {**browser_type_launch_args, 'env': {
            **os.environ, **browser_type_launch_args.get('env', {}), 'TMPDIR': str(temporary)}}


@contextmanager
def stalled_page_routes(page):
    """Own deliberately unanswered routes until their cancellation checks finish."""
    from playwright.sync_api import Error

    held = []
    try:
        yield held
    finally:
        for route in held:
            try:
                route.abort()
            except Error as error:
                if str(error) != 'Route.abort: Route is already handled!':
                    raise
                # Playwright 1.60 can cancel the driver request while leaving
                # Python's handler future unresolved. The driver has already
                # handled this request; fallback releases that local future.
                route.fallback()
        page.unroute_all(behavior='wait')


@pytest.fixture
def stalled_routes(page):
    with stalled_page_routes(page) as held:
        yield held


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

    iid = store.accounts.upsert_account("main", "cred", None, True)
    store.playlists.upsert_playlist(iid, "PLZ", "Old Mix", 3, "h", 0.0)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    with LiveServer(server):
        yield f"http://127.0.0.1:{port}"
