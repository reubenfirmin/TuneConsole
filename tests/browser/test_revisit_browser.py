"""The real prepared Revisit grid rotates, fits, and starts the chosen song."""
import pytest
from playwright.sync_api import expect

from tests.test_mode_cards_route import prepare_home
from tests.test_recommend import _seed_album
from yt_playlist.library import ratings


@pytest.mark.parametrize('width', [1440, 390])
def test_revisit_song_grid_is_small_playable_and_changes_on_the_next_visit(store, live_app, page, width):
    for i in range(20):
        store.tracks.upsert_track(f'revisit{i}', f'Forgotten song {i}', f'Artist {i}', None, 180,
            thumbnail=f'/revisit-art/{i}.svg')
        ratings.record_observations(store, 1, {f'revisit{i}': 'LIKE'}, source='sync', now=1)
    _seed_album(store, 1, 'unheard', 'Andre Sobota Collection', 1)
    _seed_album(store, 1, 'old-album', 'A record to return to', 1, played_at=1-60*86400)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    prepare_home(store, 1)
    plays = []
    page.route('**/play', lambda route: (plays.append(route.request.post_data_json['url']), route.fulfill(json={'ok': True})))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))

    def artwork(route):
        i = int(route.request.url.rsplit('/', 1)[-1].split('.')[0])
        route.fulfill(content_type='image/svg+xml', body=f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
          <path fill="hsl({i*27} 38% 32%)" d="M0 0h200v200H0z"/>
          <circle cx="{40+i*5}" cy="100" r="80" fill="none" stroke="hsl({i*27+80} 55% 70%)" stroke-width="22"/>
          <path d="M0 160L200 40" stroke="white" stroke-opacity=".45" stroke-width="2"/></svg>''')

    page.route('**/revisit-art/*.svg', artwork)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/')
    page.get_by_role('tab', name='Revisit', exact=True).click()
    section, grid = page.locator('#rediscover'), page.get_by_label('Songs to revisit', exact=True)
    expect(section.get_by_role('heading', name='Back in your orbit')).to_be_visible()
    expect(section.get_by_text('Andre Sobota Collection')).to_have_count(0)
    expect(section.get_by_text('A record to return to', exact=True)).to_be_visible()
    expect(grid.locator('.redis-song')).to_have_count(15)
    grid.scroll_into_view_if_needed()
    expect(grid.locator('img').first).to_be_visible()
    page.wait_for_function('''() => [...document.querySelectorAll('.redis-song img')]
      .every(img => img.complete && img.naturalWidth > 0)''')
    assert grid.evaluate('el => getComputedStyle(el).gridTemplateColumns.split(" ").length') == (5 if width == 1440 else 3)
    assert grid.locator('.redis-song-art').evaluate_all('els => els.every(el => el.getBoundingClientRect().width <= 129)')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.mouse.move(0, 0)
    page.evaluate('scrollTo(0, 0)')
    page.screenshot(path=f'/tmp/tuneconsole-429-revisit-{width}.png', full_page=True)
    first = grid.locator('.redis-song').evaluate_all('els => els.map(el => el.href)')
    with page.expect_response('**/play'):
        grid.locator('.redis-song').first.click()
    assert plays == [first[0]]
    page.reload()
    page.get_by_role('tab', name='Revisit', exact=True).click()
    expect(grid.locator('.redis-song')).to_have_count(15)
    assert grid.locator('.redis-song').evaluate_all('els => els.map(el => el.href)') != first
