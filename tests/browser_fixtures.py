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
from tests.home_fixture_support import (  # noqa: F401 - shared fixtures
    home_page_bytecode, home_pages_only, without_metadata_providers,
)

_BROWSER_DIR = pathlib.Path(__file__).parent / 'browser'


@pytest.fixture(scope='session')
def browser_type_launch_args(browser_type_launch_args, stdlib_test_storage):
    """Keep Chromium's singleton socket below the Unix socket path limit.

    The selected fixture volume can have an arbitrarily long path. Chromium
    creates its persistent-profile socket under TMPDIR, even when its profile
    lives elsewhere. Give the browser a short alias to the same owned storage;
    Python temporaries and all browser data still use the selected volume.
    """
    # Chromium in the CI container hangs while evaluating injected scripts when
    # TMPDIR uses /proc/<parent>/fd/<descriptor>. A filesystem symlink keeps the
    # short socket path without that cross-process procfs dependency. Only this
    # tiny alias lives on /tmp; browser data stays on the selected fixture volume.
    with tempfile.TemporaryDirectory(dir='/tmp', prefix='tc-browser-') as alias:
        temporary = pathlib.Path(alias) / 'tmp'
        temporary.symlink_to(stdlib_test_storage, target_is_directory=True)
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
def live_app(store, monkeypatch, browser_template_bytecode, without_metadata_providers):
    import uvicorn
    from yt_playlist.web.app import create_app
    from tests.conftest import FakeClient
    from yt_playlist.web import app as webapp

    # This FakeClient app consumes prepared state with automatic workers off.
    # Metadata tag matching has separate provider/startup owners; rebuilding its
    # full global matcher per app is unrelated to these rendered interactions.
    # The genre-editor browser module owns its own app and real configuration.
    templates = webapp.Jinja2Templates

    def compiled_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = browser_template_bytecode
        return result

    # Share source-checked compiled code, never an app's environment or rendered
    # responses. The first real request still pays for each cold compilation.
    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled_templates)

    iid = store.accounts.upsert_account("main", "cred", None, True)
    store.playlists.upsert_playlist(iid, "PLZ", "Old Mix", 3, "h", 0.0)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    with LiveServer(server):
        yield f"http://127.0.0.1:{port}"


@pytest.fixture(scope='session')
def browser_template_bytecode(tmp_path_factory):
    from jinja2 import FileSystemBytecodeCache

    return FileSystemBytecodeCache(str(tmp_path_factory.mktemp('browser-template-bytecode')))
