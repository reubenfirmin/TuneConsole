"""The real Home entry point must finish loading with and without rating cards."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest


STATIC = Path(__file__).resolve().parents[2] / 'src/yt_playlist/web/static'
ENTRY = 'audio-vibe-collection.js'
CARDS = {'audio-vibe-banner.js', 'audio-vibe-inspector.js', 'audio-vibe-cards.js'}


@pytest.fixture(params=[False, True], ids=['collection-only', 'rating-cards'])
def collection_app(request, isolated_app_home, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1' if request.param else '0')
    # Set the flag before constructing the server, including its template globals.
    return request.getfixturevalue('live_app'), request.param


@pytest.mark.parametrize('viewport', [{'width': 1280, 'height': 900},
                                     {'width': 1920, 'height': 1080}], ids=['desktop', 'large-desktop'])
def test_home_collection_loads_real_module_graph_on_load_and_reload(
        collection_app, page, browser, viewport, record_property):
    app, ratings = collection_app
    page.set_viewport_size(viewport)
    errors, failed, responses = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('requestfailed', lambda request: failed.append((request.url, request.failure)))
    page.on('response', lambda response: responses.append(response))
    expected = {ENTRY} | (CARDS if ratings else set())
    receipts = []

    for reload in (False, True):
        responses.clear()
        response = page.reload() if reload else page.goto(app + '/')
        assert response.status == 200
        loaded = page.evaluate('''async () => {
          const entry = document.querySelector('script[src*="audio-vibe-collection.js"]');
          if (!entry) throw Error('Home has no audio collection entry point');
          // Await the same URL used by the page, including its version. This
          // waits for its top-level dynamic imports without initializing twice.
          await import(entry.src);
          return {entry: entry.src, resources: performance.getEntriesByType('resource')
            .map(resource => resource.name)};
        }''')
        assert page.locator('[data-audio-vibe-collection]').count() == 1
        assert page.locator('[data-audio-vibe-banner]').count() == int(ratings)
        resources = {Path(urlsplit(url).path).name: url for url in loaded['resources']
                     if urlsplit(url).path.startswith('/static/')}
        assert expected <= resources.keys(), resources
        assert not (CARDS - expected) & resources.keys(), resources
        entry_version = urlsplit(loaded['entry']).query
        assert entry_version
        assert all(urlsplit(resources[name]).query == entry_version for name in expected)
        assert not errors, errors
        assert not [(url, reason) for url, reason in failed if '/static/' in url], failed
        assert not [(response.url, response.status) for response in responses
                    if '/static/' in response.url and response.status >= 400]

        served = {}
        for name in expected:
            matching = [response for response in responses if response.url == resources[name]]
            assert matching, (name, resources)
            # Playwright exposes the cached response body on an ordinary reload.
            body = matching[-1].body()
            digest = hashlib.sha256(body).hexdigest()
            assert digest == hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
            served[name] = {'url': matching[-1].url, 'sha256': digest,
                            'status': matching[-1].status}
        receipts.append({'reload': reload, 'assets': served})

    record_property('audio_collection_assets', json.dumps({
        'browser': browser.version, 'ratings': ratings, 'viewport': viewport, 'loads': receipts}))
