"""Persist a playlist rename through the rendered detail page."""
import socket
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import FakeClient
from tests.live_server import LiveServer
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def live_playlist_app(store):
    iid = store.accounts.upsert_account("Main", "cred", None, True)
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 2, "h", 1.0)
    tracks = [store.tracks.upsert_track(f"v{i}", title, artist, "Alb", 200, 1)
              for i, title, artist in [(0, "Song A", "Artist X"), (1, "Song B", "Artist Y")]]
    store.playlists.set_playlist_tracks(pid, tracks)
    client = FakeClient(tracks={"PL1": [{"videoId": f"v{i}", "setVideoId": f"sv{i}"} for i in range(2)]})
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1.0)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        with LiveServer(server, sockets=[sock]):
            yield {"base": f"http://127.0.0.1:{port}", "pid": pid, "store": store, "client": client}


@pytest.mark.parametrize('failed_first', [False])
def test_rename_playlist_updates_heading(live_playlist_app, page, failed_first):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    page.get_by_role("heading", name="Mix").click()
    inp = page.locator("input.title-input")
    inp.fill("Renamed Mix")
    inp.press("Enter")
    expect(page.get_by_role("heading", name="Renamed Mix")).to_be_visible()
    page.goto(f"{base}/playlist/{pid}")
    expect(page.get_by_role("heading", name="Renamed Mix")).to_be_visible()


@pytest.mark.parametrize('width', [1280, 1920])
def test_mix_name_matches_table_editor_and_ordinary_heading(live_playlist_app, page, width):
    case = live_playlist_app
    store, provider, base = case['store'], case['client'], case['base']
    ordinary = store.playlists.get_playlist(case['pid'])
    title = 'More in your wheelhouse - November 9 2026 #143'
    mix = store.playlists.upsert_playlist(ordinary.account_id, 'PLG', title, 2, 'g', 1)
    tracks = []
    for i, color in enumerate(['395ebc', 'bd7648']):
        tracks.append(store.tracks.upsert_track(f'v{i}', ['Song A', 'Song B'][i],
            ['Artist X', 'Artist Y'][i], 'Alb', 200, 1,
            thumbnail=f'https://fixture-art.invalid/{color}.svg'))
    store.playlists.set_playlist_tracks(mix, tracks)
    store.playlists.mark_generated_playlist('PLG')
    store.playlist_recipes.set_recipe('PLG', {'model': 'mode', 'lane': 'wheelhouse', 'vibe': 'concentrate'})
    provider._tracks['PLG'] = list(provider._tracks['PL1'])
    page.route('https://fixture-art.invalid/**', lambda route: route.fulfill(
        content_type='image/svg+xml', body=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400">'
        f'<rect width="400" height="400" fill="#{route.request.url.rsplit("/", 1)[1][:6]}"/>'
        '<circle cx="200" cy="200" r="130" fill="#132845"/>'
        '<circle cx="200" cy="200" r="80" fill="#d8bb87"/></svg>'))
    assets = Path(__file__).resolve().parents[2] / 'src/yt_playlist/web/static'
    served = {}

    def loaded(response):
        name = response.url.split('?')[0].rsplit('/', 1)[-1]
        if name in {'library-entities.css', 'library-playlists.js'}:
            served[name] = response.body()

    page.on('response', loaded)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(f'{base}/playlist/{ordinary.id}')
    heading = page.locator('#pl-title h1')
    expect(heading).to_have_text('Mix')
    expect(page.locator('.pl-generated-meta')).to_have_count(0)
    page.screenshot(path=f'/tmp/tuneconsole-1205-ordinary-{width}.png')

    page.goto(f'{base}/playlists')
    page.get_by_role('button', name='Generated playlists').click()
    page.get_by_role('link', name=title, exact=True).click()
    expect(heading).to_have_text('More in your wheelhouse')
    expect(page.locator('.pl-vibe-badge')).to_have_text('Focus')
    expect(page.locator('.pl-type-badge')).to_have_text('More in your wheelhouse')
    expect(page.locator('.pl-version')).to_have_text('Version 143')
    expect(page.locator('.pl-generated-date')).to_have_text('November 9, 2026')
    assert page.locator('.pl-generated-meta').bounding_box()['y'] < heading.bounding_box()['y']
    page.screenshot(path=f'/tmp/tuneconsole-1205-generated-{width}.png')
    heading.click()
    editor = page.locator('input.title-input')
    expect(editor).to_be_visible()
    expect(editor).to_have_value(title)
    editor.press('Escape')
    expect(heading).to_be_visible()
    assert not provider.edited
    heading.click()
    expect(editor).to_be_visible()
    editor.press('Enter')
    expect(heading).to_be_visible()
    expect(heading).to_have_text('More in your wheelhouse')
    assert store.playlists.get_playlist(mix).title == title

    heading.click()
    custom = 'Malimbas in socks - #1'
    editor.fill(custom)
    editor.press('Enter')
    expect(heading).to_have_text(custom)
    expect(page.locator('.pl-generated-meta')).to_have_count(0)
    page.screenshot(path=f'/tmp/tuneconsole-1205-renamed-{width}.png')
    page.goto(f'{base}/playlists')
    page.get_by_role('link', name=custom, exact=True).click()
    expect(heading).to_have_text(custom)
    heading.click()
    expect(editor).to_be_visible()
    expect(editor).to_have_value(custom)
    editor.press('Escape')
    page.reload()
    expect(heading).to_have_text(custom)
    assert store.playlists.get_playlist(mix).title == custom
    assert set(served) == {'library-entities.css', 'library-playlists.js'}
    assert all(content == (assets / name).read_bytes() for name, content in served.items())
