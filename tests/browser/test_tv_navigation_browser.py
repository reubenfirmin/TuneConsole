"""Real page navigation must retain the iframe's browsing context, not just its ID."""
import pytest
from playwright.sync_api import expect

from tests.browser.test_tv_browser import tv_ui
from tests.browser.test_clusters_browser import cluster_library
from tests.browser.test_tv_stream_browser import MODEL, publish_pool
from tests.test_trends_recap_route import _STORY
from yt_playlist.rec import home_prepared


def start_tv(page, live_app, store):
    publish_pool(store, live_app, 60)
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    page.frame_locator('.tv-player iframe').get_by_text('Controlled YouTube player boundary').wait_for()
    page.evaluate('''window.originalTvPlayer = window.tvPlayer;
        window.originalTvFrame = document.querySelector('.tv-player iframe');
        window.originalTvFrame.contentWindow.sessionMarker = 'still-connected';
        window.tvPlayer.position = 37;''')


def assert_same_player(page):
    assert page.evaluate('''window.tvPlayer === window.originalTvPlayer &&
        document.querySelector('.tv-player iframe') === window.originalTvFrame &&
        window.originalTvFrame.contentWindow.sessionMarker === 'still-connected' ''')
    assert page.evaluate(f'{MODEL}.playing && window.tvPlaybackControls.active()')
    assert not page.evaluate('window.tvCalls.some(call => call[0] === "destroy")')


def clear_mix_preparation(store):
    for surface in ('home_prepared', 'home_tuned', 'home_choice'):
        store.rec_proposals.put_proposals(surface, {}, 1)
    # Use the actual serving loader, rather than assuming that clearing one
    # proposal surface makes Home stop finding prepared menus.
    assert home_prepared.load(store, 1) is None


def assert_no_prepared_mix_offers(page, store):
    assert home_prepared.load(store, 1) is None
    board = page.locator('#home-mode-cards')
    expect(board).to_have_count(1)
    # Home deliberately keeps four actionable pool placeholders when there is
    # no prepared menu. They share .gen-card with real offers; they have no
    # generated playlist body, track list or saved recipe.
    expect(board.locator('.gen-card:not(.fresh-pool)')).to_have_count(0)
    expect(board.locator('.gen-card.fresh-pool')).to_have_count(4)
    expect(board.locator('.gen-card-body, .gen-row, [data-recipe]')).to_have_count(0)
    expect(page.get_by_text('Your next mixes will appear as your library is prepared.', exact=True)).to_have_count(1)


@pytest.mark.parametrize('width', [1440, 390])
def test_tv_opens_and_returns_without_prepared_mix_offers(page, live_app, tv_ui, store, width):
    page.set_viewport_size({'width': width, 'height': 900})
    publish_pool(store, live_app, 60)
    clear_mix_preparation(store)
    # The TV boundary fixture prepared sources before this test; its mix offer
    # is deliberately invalidated before rendering Home or opening the TV tab.
    assert not store.rec_proposals.get_proposals('home_prepared')
    assert page.request.get(live_app + '/home/tv').json()['ready'] is True
    page.goto(live_app + '/#tv')
    tv_tab = page.get_by_role('tab', name='TV', exact=True)
    assert_no_prepared_mix_offers(page, store)
    expect(tv_tab).to_be_visible()
    expect(tv_tab).to_have_attribute('aria-selected', 'true')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    page.frame_locator('.tv-player iframe').get_by_text('Controlled YouTube player boundary').wait_for()
    page.evaluate('''window.originalTvPlayer = window.tvPlayer;
        window.originalTvFrame = document.querySelector('.tv-player iframe');
        window.originalTvFrame.contentWindow.sessionMarker = 'still-connected';
        window.tvPlayer.position = 37;''')
    playlist = page.evaluate(f'{MODEL}.playlistId')
    page.locator('.topbar').get_by_role('link', name='Playlists', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Playlists')
    page.locator('.global-now-playing .hs-source').click()
    assert_no_prepared_mix_offers(page, store)
    expect(tv_tab).to_be_visible()
    expect(page.locator('.tv-player iframe')).to_be_visible()
    expect(page.get_by_role('button', name='Adjust TV parameters')).to_be_visible()
    assert_same_player(page)
    assert page.evaluate(f'{MODEL}.playlistId') == playlist
    assert page.evaluate('window.tvPlayer.position') == 37
    page.wait_for_function('''() => {
        const player = document.querySelector('.tv-home').getBoundingClientRect();
        const slot = document.querySelector('.tv-slot').getBoundingClientRect();
        return Math.abs(player.x - slot.x) < 1 && Math.abs(player.y - slot.y) < 1 &&
            Math.abs(player.width - slot.width) < 1 && player.height > 220 &&
            document.documentElement.scrollWidth <= innerWidth;
    }''')
    page.screenshot(path=f'/tmp/tuneconsole-439-empty-mixes-{width}.png')


@pytest.mark.parametrize('width', [1440, 390])
def test_tv_survives_pages_controls_history_and_return_to_tv(page, live_app, tv_ui, store, width):
    page.set_viewport_size({'width': width, 'height': 1000})
    start_tv(page, live_app, store)
    playlist = page.evaluate(f'{MODEL}.playlistId')
    nav = page.locator('.topbar')
    for name in ('Playlists', 'Artists', 'Albums', 'Charts'):
        nav.get_by_role('link', name=name, exact=True).click()
        expect(page.locator('main h1')).to_have_text(name)
        assert_same_player(page)
        expect(page.locator('.tv-home')).to_be_hidden()
        expect(page.locator('.tv-backdrop')).to_be_hidden()
        expect(nav.get_by_role('link', name=name, exact=True)).to_have_class('active')
        expect(page.locator('.global-now-playing .hs-source')).to_have_text('Back to TV')
    assert page.evaluate('window.tvPlayer.position') == 37
    controls = page.locator('.global-now-playing')
    controls.get_by_role('button', name='Pause', exact=True).click()
    page.wait_for_function(f'!{MODEL}.playing')
    controls.get_by_role('button', name='Play', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    controls.get_by_role('button', name='Next / skip', exact=True).click()
    page.wait_for_function('window.tvPlayer.id === "visual00001"')
    page.evaluate('window.tvPlayer.options.events.onStateChange({data:0,target:window.tvPlayer})')
    page.wait_for_function('window.tvPlayer.id === "visual00002"')
    # Back/Forward must replace page content without replacing the document.
    page.evaluate('history.back()')
    expect(page.locator('main h1')).to_have_text('Albums')
    assert_same_player(page)
    page.evaluate('history.forward()')
    expect(page.locator('main h1')).to_have_text('Charts')
    assert_same_player(page)
    # Re-enter Home while its formerly populated mix preparation is missing.
    # A running TV session and its normal tab must survive that transition.
    clear_mix_preparation(store)
    nav.get_by_role('link', name='Home', exact=True).click()
    assert_no_prepared_mix_offers(page, store)
    expect(page.locator('.dash-tv-dot')).to_be_visible()
    expect(page.get_by_role('tab', name='TV', exact=True)).to_have_attribute('aria-description', 'TV is playing')
    expect(page.locator('.dash-tv-dot')).to_have_css('background-color', 'rgb(57, 135, 229)')
    assert_same_player(page)
    page.screenshot(path=f'/tmp/tuneconsole-tv-playing-dot-{width}.png')
    controls.locator('.hs-source').click()
    expect(page.locator('.tv-player iframe')).to_be_visible()
    expect(page.locator('.tv-backdrop')).to_be_visible()
    expect(page.locator('.dash-tv-dot')).to_be_hidden()
    assert_same_player(page)
    assert page.evaluate(f'{MODEL}.playlistId') == playlist
    assert page.evaluate('window.tvPlayer.id') == 'visual00002'
    expect(page.locator('.tv-film[aria-current="true"]')).to_have_attribute('aria-label', 'Now playing: Video 2')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.wait_for_function('''() => {
        const player = document.querySelector('.tv-home').getBoundingClientRect();
        const slot = document.querySelector('.tv-slot').getBoundingClientRect();
        return Math.abs(player.x - slot.x) < 1 && Math.abs(player.y - slot.y) < 1 &&
            Math.abs(player.width - slot.width) < 1 && player.height > 220;
    }''')
    page.screenshot(path=f'/tmp/tuneconsole-tv-navigation-{width}.png')


def test_tv_survives_scripted_navigation_and_lazily_loaded_page_assets(page, live_app, tv_ui, store):
    start_tv(page, live_app, store)
    page.evaluate('location.assign("/taste")')
    expect(page.locator('main h1')).to_have_text('Taste Model')
    expect(page.get_by_role('tab', name='Taste profile', exact=True)).to_be_visible()
    page.get_by_role('tab', name='Taste profile', exact=True).click()
    assert_same_player(page)
    # Returning to the same scripted page must not redeclare global script state.
    for path in ('/privacy', '/taste', '/privacy'):
        page.evaluate('path => location.assign(path)', path)
        expect(page.locator('main h1')).to_have_text('Privacy policy' if path == '/privacy' else 'Taste Model')
        assert_same_player(page)
    page.locator('.global-now-playing .hs-source').click()
    expect(page.locator('.tv-player iframe')).to_be_visible()
    assert_same_player(page)


def test_failed_page_navigation_leaves_tv_running_and_can_be_retried(page, live_app, tv_ui, store):
    start_tv(page, live_app, store)
    page.route('**/playlists', lambda route: route.fulfill(status=503, body='Unavailable'))
    page.locator('.topbar').get_by_role('link', name='Playlists', exact=True).click()
    expect(page.locator('#navigation-error')).to_be_visible()
    assert page.url == live_app + '/#tv'
    assert_same_player(page)
    page.unroute('**/playlists')
    page.locator('.topbar').get_by_role('link', name='Playlists', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Playlists')
    expect(page.locator('#navigation-error')).to_have_count(0)
    assert_same_player(page)


def test_tv_survives_post_forms_htmx_refresh_and_redirect(page, live_app, tv_ui, store):
    start_tv(page, live_app, store)
    privacy = page.request.get(live_app + '/privacy').text()
    posted = []
    def submit(route):
        posted.append(route.request.method)
        route.fulfill(content_type='text/html', body=privacy)
    page.route('**/tv-test-submit', submit)
    page.evaluate('''const form = document.createElement('form');
        form.action = '/tv-test-submit'; form.method = 'POST';
        form.innerHTML = '<input name="q" value="music"><button>Submit page</button>';
        document.querySelector('main').append(form);''')
    page.get_by_role('button', name='Submit page', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Privacy policy')
    assert posted == ['POST']
    assert_same_player(page)
    # Use a real current page as the GET target of the server-requested refresh.
    page.evaluate('location.assign("/privacy")')
    expect(page).to_have_url(live_app + '/privacy')
    page.wait_for_function('!document.querySelector("main").hasAttribute("aria-busy")')
    for endpoint, headers, expected in (
        ('/tv-test-refresh', {'HX-Refresh': 'true'}, 'Privacy policy'),
        ('/tv-test-redirect', {'HX-Redirect': '/albums'}, 'Albums'),
    ):
        page.route('**' + endpoint, lambda route, _request, headers=headers: route.fulfill(headers=headers))
        page.evaluate('''endpoint => {
            const button = document.createElement('button'); button.textContent = 'Server navigation';
            button.setAttribute('hx-post', endpoint); document.querySelector('main').append(button);
            htmx.process(button);
        }''', endpoint)
        with page.expect_request(lambda request: request.method == 'GET' and request.url.endswith('/albums' if expected == 'Albums' else '/privacy')):
            page.get_by_role('button', name='Server navigation', exact=True).click()
        expect(page.locator('main h1')).to_have_text(expected)
        expect(page.locator(f'button[hx-post="{endpoint}"]')).to_have_count(0)
        assert_same_player(page)


def test_tv_preserves_page_mounting_and_cleanup_for_tools(page, live_app, tv_ui, store, cluster_library):
    start_tv(page, live_app, store)
    for path, selector in (
        ('/clusters', '#cluster-canvas canvas'), ('/setup', '.page-setup'),
        ('/audio-preview', '[data-audio-preview]'), ('/privacy', 'main h1'),
        ('/audio-preview', '[data-audio-preview]'), ('/clusters', '#cluster-canvas canvas'),
        ('/privacy', 'main h1'),
    ):
        page.evaluate('path => location.assign(path)', path)
        expect(page.locator(selector).first).to_be_visible()
        page.wait_for_function('!document.querySelector("main").hasAttribute("aria-busy")')
        assert_same_player(page)
    page.set_viewport_size({'width': 900, 'height': 800})
    page.locator('.global-now-playing .hs-source').click()
    expect(page.locator('.tv-player iframe')).to_be_visible()
    assert_same_player(page)


def test_tv_survives_standalone_screens_and_returns_with_browser_back(page, live_app, tv_ui, store):
    start_tv(page, live_app, store)
    for _ in range(2):
        page.evaluate('location.assign("/home/generating")')
        expect(page.locator('main .gen-stage')).to_be_visible()
        page.wait_for_function('!document.querySelector("main").hasAttribute("aria-busy")')
        assert_same_player(page)
        expect(page.locator('.tv-home')).to_be_hidden()
        page.evaluate('history.back()')
        expect(page.locator('.tv-player iframe')).to_be_visible()
        assert_same_player(page)


def test_tv_survives_recap_and_cleans_up_its_keyboard_handler(page, live_app, tv_ui, store):
    store.rec_proposals.put_proposals('trend_rollups', {'story': _STORY}, 1)
    start_tv(page, live_app, store)
    for _ in range(2):
        page.evaluate('location.assign("/trends/story/2026-06")')
        expect(page.locator('.reel')).to_be_visible()
        expect(page.locator('body')).to_have_class('reel-body app-standalone')
        assert_same_player(page)
        page.wait_for_function('''() => {
            const reel = document.querySelector('.reel').getBoundingClientRect();
            const dock = document.querySelector('.global-now-playing-slot').getBoundingClientRect();
            return reel.x === 0 && reel.y === 0 && reel.width === innerWidth
                && Math.abs(reel.bottom - dock.top) < 1;
        }''')
        page.keyboard.press('ArrowRight')
        expect(page.locator('.slide-numbers')).to_be_visible()
        page.get_by_role('link', name='Close', exact=True).click()
        expect(page.locator('.topbar')).to_be_visible()
        expect(page.locator('body')).not_to_have_class('reel-body')
        page.locator('.global-now-playing .hs-source').click()
        expect(page.locator('.tv-player iframe')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page).to_have_url(live_app + '/#tv')
        assert_same_player(page)


@pytest.mark.parametrize('destination', ['/home/generating', '/trends/story/2026-06'])
@pytest.mark.parametrize('width,height', [(1440, 900), (390, 600)])
def test_standalone_tv_controls_remain_usable_without_covering_the_page(
        page, live_app, tv_ui, store, destination, width, height):
    page.set_viewport_size({'width': width, 'height': height})
    store.rec_proposals.put_proposals('trend_rollups', {'story': _STORY}, 1)
    start_tv(page, live_app, store)
    page.evaluate('path => location.assign(path)', destination)
    screen = page.locator('main > .gen-stage' if 'generating' in destination else '.reel')
    expect(screen).to_be_visible()
    controls = page.locator('.global-now-playing')
    expect(controls.get_by_role('button', name='Pause', exact=True)).to_be_visible()
    expect(controls.locator('.hs-source')).to_have_text('Back to TV')
    expect(controls.locator('.hs-now')).to_have_css('display', 'grid')
    expect(controls.locator('.hs-playpause svg:visible')).to_have_css('width', '20px')
    page.wait_for_function('''() => {
        const screen = document.querySelector('main > .gen-stage, .reel').getBoundingClientRect();
        const dock = document.querySelector('.global-now-playing-slot').getBoundingClientRect();
        return Math.abs(screen.bottom - dock.top) < 1 && screen.height > 250 && dock.bottom <= innerHeight;
    }''')
    controls.get_by_role('button', name='Pause', exact=True).click()
    page.wait_for_function(f'!{MODEL}.playing')
    controls.get_by_role('button', name='Play', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    controls.get_by_role('button', name='Next / skip', exact=True).click()
    page.wait_for_function('window.tvPlayer.id === "visual00001"')
    assert_same_player(page)
    page.screenshot(path=f'/tmp/tuneconsole-439-standalone-{width}-' + ('generation' if 'generating' in destination else 'recap') + '.png')
    controls.locator('.hs-source').click()
    expect(page.locator('.tv-player iframe')).to_be_visible()
    assert_same_player(page)
    expect(page.locator('.global-now-playing-slot')).to_have_css('position', 'static')


def test_same_page_refresh_preserves_tv_after_a_ui_action(page, live_app, tv_ui, store):
    start_tv(page, live_app, store)
    page.locator('.topbar').get_by_role('link', name='Playlists', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Playlists')
    page.evaluate('document.querySelector("main").dataset.beforeRefresh = "true"')
    page.evaluate('location.replace(location.href)')
    page.wait_for_function('!document.querySelector("main").dataset.beforeRefresh')
    assert_same_player(page)


def test_saved_tv_playlist_links_can_select_another_session_after_navigation(page, live_app, tv_ui, store):
    start_tv(page, live_app, store)
    playlists = [page.request.post(live_app + '/home/tv/play').json() for _ in range(2)]
    for playlist in playlists:
        page.evaluate('location.assign("/playlists")')
        expect(page.locator('main h1')).to_have_text('Playlists')
        page.evaluate('id => location.assign("/?tv_playlist=" + id + "#tv")', playlist['playlist_id'])
        page.wait_for_function(f'id => {MODEL}.playlistId === id && {MODEL}.playing', arg=playlist['playlist_id'])
        expect(page.locator('.tv-player iframe')).to_be_visible()
        assert page.evaluate('window.tvPlayer.id') == playlist['items'][0]['video_id']


def test_newer_navigation_wins_and_downloads_leave_tv_connected(page, live_app, tv_ui, store):
    start_tv(page, live_app, store)
    pending = []
    page.route('**/artists', lambda route: pending.append(route))
    artists = page.request.get(live_app + '/artists').text()
    nav = page.locator('.topbar')
    with page.expect_request('**/artists'):
        nav.get_by_role('link', name='Artists', exact=True).click()
    nav.get_by_role('link', name='Albums', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Albums')
    pending.pop().fulfill(content_type='text/html', body=artists)
    page.wait_for_timeout(100)
    expect(page.locator('main h1')).to_have_text('Albums')
    assert_same_player(page)
    page.route('**/tv-test-download', lambda route: route.fulfill(content_type='text/plain', body='music'))
    page.evaluate('''const link = document.createElement('a'); link.href = '/tv-test-download';
        link.download = 'tv.txt'; link.textContent = 'Download'; document.querySelector('main').append(link);''')
    with page.expect_download() as downloaded:
        page.get_by_role('link', name='Download', exact=True).click()
    assert downloaded.value.suggested_filename == 'tv.txt'
    assert_same_player(page)


def test_navigation_without_a_tv_session_uses_normal_document_loads(page, live_app, tv_ui):
    page.goto(live_app + '/#tv')
    page.evaluate('window.initialTvDocument = true')
    page.locator('.topbar').get_by_role('link', name='Playlists', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Playlists')
    assert page.evaluate('window.initialTvDocument === undefined')
