"""Album HTTP mutations must leave their production bridge WebSocket loop responsive."""
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import json
import logging
from types import SimpleNamespace
import time
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from anyio import fail_after
import pytest

from yt_playlist.core.bridge import Bridge
from yt_playlist.core.identities import build_client
from yt_playlist.web.routes import bridge as bridge_routes, collection


def _album_response():
    header = {
        'title': {'runs': [{'text': 'The Album'}]},
        'subtitle': {'runs': [{'text': 'Album'}, {'text': ' • '}, {'text': '2024'}]},
        'secondSubtitle': {'runs': [{'text': '0 songs'}]},
        'thumbnail': {'musicThumbnailRenderer': {'thumbnail': {'thumbnails': []}}},
        'buttons': [{'musicPlayButtonRenderer': {'playNavigationEndpoint': {
            'watchPlaylistEndpoint': {'playlistId': 'PLAUDIO'}}}}],
    }
    return {'contents': {'twoColumnBrowseResultsRenderer': {
        'tabs': [{'tabRenderer': {'content': {'sectionListRenderer': {
            'contents': [{'musicResponsiveHeaderRenderer': header}]}}}}],
        'secondaryContents': {'sectionListRenderer': {
            'contents': [{'musicShelfRenderer': {'contents': []}}]}},
    }}}


def _receive_json(ws, timeout=5):
    async def receive():
        # TestClient's public receive_json has no deadline. Bound its underlying
        # async stream so a silent WebSocket cannot strand the test coordinator.
        with fail_after(timeout):
            return await ws._send_rx.receive()

    message = ws.portal.call(receive)
    ws._raise_on_close(message)
    return json.loads(message['text'])


def _request_frame(ws, pending, timeout=5):
    # Keepalive frames share the same socket and can arrive before a queued request.
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        if pending.done():
            response = pending.result()
            pytest.fail(f'HTTP operation finished before its bridge request: {response.status_code}: {response.text}')
        frame = _receive_json(ws, remaining)
        if 'id' in frame:
            return frame
    raise TimeoutError('Timed out waiting for a bridge request frame')


@pytest.mark.parametrize('saved', [True, False], ids=['save', 'unsave'])
@pytest.mark.parametrize('failure', [None, 'lookup', 'rating', 'dispatch'],
                         ids=['success', 'lookup-error', 'rating-error', 'early-http-error'])
def test_album_operation_round_trips_through_live_bridge_without_blocking_loop(store, monkeypatch, saved, failure):
    iid = store.identities.upsert_identity('main', 'bridge', None, True)
    if not saved:
        store.collection.add_saved_album({'browse': 'MPREb_x', 'title': 'The Album'},identity_id=iid)
    before = store.collection.get_saved_albums()
    bridge = Bridge()
    original_execute = bridge.execute
    # Keep regressions bounded: the production scheduler and blocking future remain unchanged.
    def bounded_execute(method, url, body, timeout=30, *, cancellable=False):
        if failure == 'dispatch':
            raise TypeError('Controlled failure before a bridge frame is sent')
        return original_execute(method, url, body, min(timeout, 3), cancellable=cancellable)

    monkeypatch.setattr(bridge, 'execute', bounded_execute)
    client = build_client(SimpleNamespace(brand_account_id=None), bridge)
    ctx = SimpleNamespace(store=store, bridge=bridge, client_provider=lambda: {iid: client},
                          templates=Jinja2Templates(directory='src/yt_playlist/web/templates'),
                          logger=logging.getLogger(__name__))
    app = FastAPI()
    app.include_router(collection.build(ctx))
    app.include_router(bridge_routes.build(ctx))

    @app.get('/health')
    async def health():
        return {'ok': True}

    # Accelerate only the WS route's keepalive clock, without patching asyncio globally.
    route_asyncio = SimpleNamespace(**vars(asyncio))
    route_asyncio.sleep = lambda delay: asyncio.sleep(min(delay, 0.01))
    monkeypatch.setattr(bridge_routes, 'asyncio', route_asyncio)
    with TestClient(app) as http:
        with http.websocket_connect('/bridge/ws', headers={'origin': bridge_routes.EXTENSION_ORIGIN}) as ws:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(http.post, '/collection/' + ('save-album' if saved else 'unsave-album'),
                                      data={'browse_id': 'MPREb_x'})
                if failure == 'dispatch':
                    with pytest.raises(pytest.fail.Exception, match='finished before its bridge request: 422'):
                        _request_frame(ws, pending)
                    return
                lookup = _request_frame(ws, pending)
                assert lookup['body']['browseId'] == 'MPREb_x'
                assert http.get('/health').json() == {'ok': True}
                assert not pending.done() and store.collection.get_saved_albums() == before
                assert _receive_json(ws) == {'ping': 1}  # Keepalives run during the blocked lookup.
                ws.send_json({'id': lookup['id'], 'status': 403 if failure == 'lookup' else 200,
                              'body': json.dumps({'error': {'message': 'lookup refused'}}
                                                 if failure == 'lookup' else _album_response())})
                if failure != 'lookup':
                    rating = _request_frame(ws, pending)
                    assert urlsplit(rating['url']).path.endswith('/like/' + ('like' if saved else 'removelike'))
                    assert rating['body']['target']['playlistId'] == 'PLAUDIO'
                    assert http.get('/health').status_code == 200
                    assert not pending.done() and store.collection.get_saved_albums() == before
                    assert _receive_json(ws) == {'ping': 1}
                    ws.send_json({'id': rating['id'], 'status': 403 if failure == 'rating' else 200,
                                  'body': json.dumps({'error': {'message': 'rating refused'}}
                                                     if failure == 'rating' else {})})
                response = pending.result(timeout=5)
    if failure:
        assert response.status_code == 422 and response.headers['hx-reswap'] == 'none'
        assert 'YouTube refused' in response.text
        assert store.collection.get_saved_albums() == before
    else:
        assert response.status_code == 200 and response.headers['hx-refresh'] == 'true'
        assert bool(store.collection.get_saved_albums()) == saved


def test_frame_wait_is_bounded_even_without_keepalives():
    app = FastAPI()

    @app.websocket('/silent')
    async def silent(ws: bridge_routes.WebSocket):
        await ws.accept()
        await asyncio.Event().wait()

    with TestClient(app) as http, http.websocket_connect('/silent') as ws:
        with pytest.raises(TimeoutError):
            _request_frame(ws, Future(), timeout=0.05)
