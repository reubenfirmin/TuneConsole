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


def assert_header_layout(page):
    boxes = page.locator('.pl-head').evaluate('''hero => {
      const box = selector => hero.querySelector(selector)?.getBoundingClientRect().toJSON();
      const style = getComputedStyle(hero);
      return {hero: hero.getBoundingClientRect().toJSON(), title: box('h1'),
        actions: box('.pl-actions'), tools: box('.head-tools'), meta: box('.pl-generated-meta'),
        left: parseFloat(style.paddingLeft), right: parseFloat(style.paddingRight),
        top: parseFloat(style.paddingTop), bottom: parseFloat(style.paddingBottom)};
    }''')
    hero, title, actions, tools = (boxes[key] for key in ('hero', 'title', 'actions', 'tools'))
    assert title['left'] == pytest.approx(hero['left'] + boxes['left'], abs=1)
    assert title['right'] == pytest.approx(hero['right'] - boxes['right'], abs=1)
    assert actions['bottom'] == pytest.approx(hero['bottom'] - boxes['bottom'], abs=1)
    assert tools['bottom'] < title['top'] < title['bottom'] < actions['top']
    top_row_bottom = tools['bottom']
    if boxes['meta']:
        meta = boxes['meta']
        assert meta['right'] < tools['left']
        assert meta['y'] + meta['height'] / 2 == pytest.approx(tools['y'] + tools['height'] / 2, abs=1)
        top_row_bottom = max(top_row_bottom, meta['bottom'])
    assert title['y'] + title['height'] / 2 == pytest.approx((top_row_bottom + actions['top']) / 2, abs=1)


@pytest.fixture
def live_playlist_app(store):
    iid = store.accounts.upsert_account("Main", None, True)
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


@pytest.mark.parametrize('width', [1000, 1180, 1600])
def test_playlist_columns_stay_at_the_viewport_edge_while_scrolling(live_playlist_app, page, tmp_path, width):
    base, pid, store = (live_playlist_app[key] for key in ('base', 'pid', 'store'))
    tracks = [store.tracks.upsert_track(f'pin{i}', f'Song {i:02}', 'Artist', 'Album', 200, 1)
              for i in range(36)]
    store.playlists.set_playlist_tracks(pid, tracks)
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(f'{base}/playlist/{pid}')
    head = page.locator('.playlist-track-card thead')
    first_row = page.locator('#playlist-track-body tr').first
    expect(first_row).to_contain_text('Song 00')
    # Fixed side navigation can be shorter than the window. It still takes up
    # no space above the content.
    page.locator('.topbar').evaluate('''bar => {
      if (getComputedStyle(bar).position === 'fixed') bar.style.bottom = '8px';
      dispatchEvent(new Event('resize'));
    }''')
    at_start = head.bounding_box()
    assert at_start['y'] + at_start['height'] <= first_row.bounding_box()['y'] + 1

    for distance in (240, 530, 110):
        page.evaluate('(y) => window.scrollTo({top: y, behavior: "instant"})', at_start['y'] + distance)
        pinned = head.bounding_box()
        top = page.evaluate('''() => Math.max(0, ...Array.from(document.querySelectorAll('.setup-status, .topbar'))
          .filter(element => getComputedStyle(element).position === 'sticky' && element.offsetHeight)
          .map(element => element.getBoundingClientRect().bottom))''')
        assert pinned['y'] == pytest.approx(top, abs=1)
        assert pinned['height'] == pytest.approx(at_start['height'], abs=1)
        song = head.get_by_role('button', name='Song', exact=True).bounding_box()
        assert song['y'] < pinned['y'] + pinned['height']
        assert song['x'] == pytest.approx(first_row.locator('td').nth(3).bounding_box()['x'], abs=1)
        assert page.locator('#playlist-track-body').evaluate('''(body, bottom) => {
          const button = Array.from(body.querySelectorAll('.rowmenu button')).find(button => {
            const {top, bottom: end, width, height} = button.getBoundingClientRect();
            return width && height && top > bottom + 2 && end < innerHeight - 100;
          });
          if (!button) return false;
          const {x, y, width, height} = button.getBoundingClientRect();
          return button.contains(document.elementFromPoint(x + width / 2, y + height / 2));
        }''', pinned['y'] + pinned['height'])
    page.screenshot(path=tmp_path / 'pinned-playlist-header.png')

    page.evaluate('window.scrollTo({top: 0, behavior: "instant"})')
    assert head.bounding_box()['y'] == pytest.approx(page.locator('.playlist-track-card table').bounding_box()['y'], abs=1)
    assert head.bounding_box()['y'] > top


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


@pytest.mark.parametrize('completion_before_add,failed_refresh', [(False, False), (True, False), (False, True)])
def test_suggestion_metadata_survives_an_older_completion(
        live_playlist_app, page, stalled_routes, completion_before_add, failed_refresh):
    case = live_playlist_app
    store, pid, base = case['store'], case['pid'], case['base']
    original = store.tracks.track_ids_for_videos(['v0', 'v1'])
    for track_id in original.values():
        store.tracks.set_track_genre(track_id, 'Rock')
        store.tracks.set_track_year(track_id, '2001')
    added = store.tracks.upsert_track('bonus', 'Bonus song', 'Artist X', 'Alb', 200, 1)
    store.tracks.set_track_genre(added, 'Rock')
    source = store.playlists.upsert_playlist(store.playlists.get_playlist(pid).account_id,
        'PLSOURCE', 'Source', 2, 's', 1)
    store.playlists.set_playlist_tracks(source, [original['v0'], added])
    case['client']._catalog['bonus'] = {
        'videoId': 'bonus', 'title': 'Bonus song', 'artists': [{'name': 'Artist X'}],
        'album': {'name': 'Alb'}, 'duration_seconds': 200, 'setVideoId': 'bonus-set',
    }
    page.route('**/bridge/status', lambda route: route.fulfill(json={
        'connected': True, 'now_playing': {'video_id': 'v0', 'title': 'Song A',
            'artist': 'Artist X', 'paused': False, 'playlist_ytm_id': 'PL1'},
    }))
    cells_url = f'{base}/playlist/{pid}/cells'
    reads, initial = [], []

    def cells(route):
        reads.append(route.request)
        if len(reads) == 1:
            snapshot = route.fetch().json()
            assert snapshot['pending'] == 0
            initial.append((route, snapshot))
            stalled_routes.append(route)
        elif failed_refresh and len(reads) == 2:
            # An HTTP failure must not be mistaken for successful completion.
            route.fulfill(status=500, json={'cells': [], 'pending': 0})
        else:
            route.continue_()

    page.route(cells_url, cells)
    loaded = []
    page.on('response', lambda response: loaded.append(response.body())
        if '/static/track-tables.js?' in response.url else None)
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(f'{base}/playlist/{pid}')
    suggestion = page.locator('.tile-suggest').filter(has_text='Bonus song')
    expect(suggestion.get_by_title('Add to this playlist')).to_be_visible()
    expect(page.locator('.global-now-playing .hs-now-title')).to_have_text('Song A')
    page.evaluate('''() => {
        window.originalSong = document.querySelector('tr[data-vid="v0"]');
        window.originalDock = document.querySelector('.global-now-playing');
    }''')
    assert len(initial) == 1
    if completion_before_add:
        with page.expect_response(cells_url):
            initial[0][0].fulfill(json=initial[0][1])
        stalled_routes.remove(initial[0][0])
        # Let the JSON completion run before the user adds the suggestion.
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')

    # The suggestion is already visible when its missing metadata is discovered.
    store.tracks.set_track_genre(added, '')
    suggestion.get_by_title('Add to this playlist').click()
    new_row = page.locator('tr[data-vid="bonus"]')
    expect(new_row.locator('.genre-display-wrap .ghint')).to_be_visible()
    page.evaluate('''() => {
        const range = document.createRange();
        range.selectNodeContents(originalSong.querySelector('.ptitle'));
        getSelection().removeAllRanges(); getSelection().addRange(range);
    }''')
    if not completion_before_add:
        # A polling opportunity while the older read is held must not overlap it.
        page.wait_for_timeout(2700)
        assert len(reads) == 1
        with page.expect_response(cells_url):
            initial[0][0].fulfill(json=initial[0][1])
        stalled_routes.remove(initial[0][0])

    # Enrichment lands after insertion; polling must still reach this new row.
    store.tracks.set_track_genre(added, 'Space Rock')
    store.tracks.set_track_year(added, '1994')
    expect(new_row.locator('.gtag-label')).to_have_text('Space Rock', timeout=7000)
    expect(new_row.locator('.ydisplay')).to_have_text('1994')
    expect(page.locator('#playlist-track-count')).to_have_text('3')
    expect(page.locator('.global-now-playing .hs-now-title')).to_have_text('Song A')
    assert page.evaluate('''() => originalSong === document.querySelector('tr[data-vid="v0"]') &&
        originalDock === document.querySelector('.global-now-playing')''')
    assert page.evaluate('getSelection().toString()') == 'Song A'
    assert len(reads) >= (3 if failed_refresh else 2)
    assert loaded == [(Path(__file__).resolve().parents[2] /
        'src/yt_playlist/web/static/track-tables.js').read_bytes()]
    page.screenshot(path=f'/tmp/tuneconsole-1179-metadata-{completion_before_add}-{failed_refresh}.png')


def test_metadata_reads_cancel_on_navigation_and_resume_after_page_restore(
        live_playlist_app, page, stalled_routes):
    case = live_playlist_app
    store, pid, base = case['store'], case['pid'], case['base']
    cells_url = f'{base}/playlist/{pid}/cells'
    reads, canceled = [], []

    def cells(route):
        reads.append(route)
        if len(reads) <= 3:
            stalled_routes.append(route)
            page.evaluate('(count) => window.heldMetadataRequests = count', len(reads))
        else:
            route.continue_()

    page.route(cells_url, cells)
    page.on('requestfailed', lambda request: canceled.append(request)
        if request.url == cells_url else None)
    with page.expect_request(cells_url):
        page.goto(f'{base}/playlist/{pid}')
    page.evaluate('window.originalDock = document.querySelector(".global-now-playing")')
    # Exercise real app navigation, including a return to the same playlist URL.
    page.get_by_role('link', name='Playlists', exact=True).click()
    expect(page.get_by_role('heading', name='Playlists', exact=True)).to_be_visible()
    with page.expect_request(cells_url):
        page.get_by_role('link', name='Mix', exact=True).click()
    expect(page.get_by_role('heading', name='Mix', exact=True)).to_be_visible()
    assert len(reads) == 2
    assert canceled and canceled[0] == reads[0].request

    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))")
    page.wait_for_timeout(2700)
    assert len(reads) == 2, 'periodic reads stay paused after pagehide'
    assert len(canceled) == 2
    with page.expect_request(cells_url):
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    page.wait_for_function('window.heldMetadataRequests === 3')
    assert len(reads) == 3
    # Canceled responses must not complete the restored table's current read.
    for route in reads[:2]:
        route.fulfill(json={'cells': [], 'pending': 0})
        stalled_routes.remove(route)
    with page.expect_response(cells_url):
        reads[2].fulfill(json={'cells': [], 'pending': 2})
    stalled_routes.remove(reads[2])

    track_id = store.tracks.track_ids_for_videos(['v0'])['v0']
    store.tracks.set_track_genre(track_id, 'Jazz')
    store.tracks.set_track_year(track_id, '1997')
    row = page.locator('tr[data-vid="v0"]')
    expect(row.locator('.gtag-label')).to_have_text('Jazz', timeout=7000)
    expect(row.locator('.ydisplay')).to_have_text('1997')
    assert page.evaluate('originalDock === document.querySelector(".global-now-playing")')


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
        store.tracks.set_track_genre(tracks[-1], 'Space Rock')
        store.tracks.set_track_year(tracks[-1], '1994')
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
    assert_header_layout(page)
    page.screenshot(path=f'/tmp/tuneconsole-1205-ordinary-{width}.png')

    page.goto(f'{base}/playlists')
    page.get_by_role('button', name='Generated playlists').click()
    page.get_by_role('link', name=title, exact=True).click()
    themed_name = 'More in your wheelhouse with mostly space rock from the 1990s'
    expect(heading).to_have_text(themed_name)
    expect(page.locator('.pl-vibe-badge')).to_have_text('Focus')
    expect(page.locator('.pl-type-badge')).to_have_text('More in your wheelhouse')
    expect(page.locator('.pl-version')).to_have_text('Version 143')
    expect(page.locator('.pl-generated-date')).to_have_text('November 9, 2026')
    assert page.locator('.pl-generated-meta').bounding_box()['y'] < heading.bounding_box()['y']
    assert_header_layout(page)
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
    expect(heading).to_have_text(themed_name)
    assert_header_layout(page)
    assert store.playlists.get_playlist(mix).title == title

    heading.click()
    custom = 'Malimbas in socks - #1'
    editor.fill(custom)
    editor.press('Enter')
    expect(heading).to_have_text(custom)
    expect(page.locator('.pl-generated-meta')).to_have_count(0)
    assert_header_layout(page)
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
