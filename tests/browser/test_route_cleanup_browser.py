"""Stalled test transports must finish before Playwright closes its loop."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest

from tests.browser_fixtures import stalled_page_routes


@pytest.fixture(scope='module')
def route_origin():
    delivered = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            delivered.append((self.command, self.path))
            self.send_response(204)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', delivered
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('unroute_first', [False, True])
def test_stalled_route_cleanup_drains_without_delivering_request(
        page, route_origin, method, cancelled, unroute_first):
    origin, delivered = route_origin
    before = list(delivered)
    with stalled_page_routes(page) as held:
        page.route(origin + '/', lambda route: route.fulfill(
            content_type='text/html', body='<title>Route ownership</title>'))
        page.route('**/held', lambda route: held.append(route))
        page.goto(origin + '/')
        page.evaluate('''method => {
            window.controller = new AbortController();
            window.outcome = 'pending';
            fetch('/held', {method, signal: controller.signal})
                .then(() => {outcome = 'loaded';}, error => {outcome = error.name;});
        }''', method)
        deadline = time.monotonic() + 3
        while not held:
            assert time.monotonic() < deadline, 'The route was not intercepted'
            page.wait_for_timeout(10)
        assert len(held) == 1
        if cancelled:
            page.evaluate('controller.abort()')
            page.wait_for_function("outcome !== 'pending'", timeout=1000)
            assert page.evaluate('outcome') == 'AbortError'
        else:
            assert page.evaluate('outcome') == 'pending'
        if unroute_first:
            page.unroute('**/held')

    page.wait_for_function("outcome !== 'pending'", timeout=1000)
    assert page.evaluate('outcome') == ('AbortError' if cancelled else 'TypeError')
    assert delivered == before
    # Diagnostic ownership assertion: do not depend on later garbage collection
    # to expose a handler left awaiting its local handled_future.
    assert not [task for task in asyncio.all_tasks(page._impl_obj._loop)
                if task.get_coro().__qualname__ == 'Page._on_route']
