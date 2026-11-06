"""Real page navigation must retain the iframe's browsing context, not just its ID."""
import pytest
from playwright.sync_api import expect

from tests.browser.test_tv_browser import store, tv_ui
from tests.browser.tv_support import MODEL, publish_pool
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




@pytest.mark.parametrize('width', [1440])
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
        expect(page.locator('.account-health, #account-health-slot')).to_have_count(0)
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
    expect(page.locator('.account-health, #account-health-slot')).to_have_count(0)
    expect(page.locator('.dash-tv-dot')).to_be_visible()
    expect(page.get_by_role('tab', name='TV', exact=True)).to_have_attribute('aria-description', 'TV is playing')
    expect(page.locator('.dash-tv-dot')).to_have_css('background-color', 'rgb(57, 135, 229)')
    assert_same_player(page)
    expect(controls.locator('.hs-source')).to_be_in_viewport()
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




def test_failed_page_navigation_leaves_tv_running_and_can_be_retried(page, live_app, tv_ui, store):
    start_tv(page, live_app, store)
    page.route('**/playlists', lambda route: route.fulfill(status=503, body='Unavailable'))
    page.locator('.topbar').get_by_role('link', name='Playlists', exact=True).click()
    expect(page.locator('main')).not_to_have_attribute('aria-busy', 'true')
    expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
    assert page.url == live_app + '/#tv'
    assert_same_player(page)
    page.unroute('**/playlists')
    expect(page.locator('main h1')).to_have_text('Playlists', timeout=10000)
    expect(page.locator('#navigation-error')).to_have_count(0)
    assert_same_player(page)
