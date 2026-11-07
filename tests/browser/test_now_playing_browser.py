"""Space controls playback while respecting search and focused buttons."""
from playwright.sync_api import expect


def test_space_controls_playback_without_intercepting_search_or_focused_buttons(live_app, page):
    status = {'connected': True, 'now_playing': {
        'video_id': 'keyboard', 'title': 'Song', 'artist': 'Artist', 'paused': False}}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    commands = []

    def toggle(route):
        commands.append(route.request.url)
        status['now_playing']['paused'] = not status['now_playing']['paused']
        route.fulfill(json={'ok': True})

    page.route('**/now-playing/toggle*', toggle)
    page.goto(live_app + '/artists')
    player = page.locator('.global-now-playing')
    expect(player).to_be_visible()
    page.locator('body').click(position={'x': 600, 'y': 600})
    page.keyboard.press('Space')
    expect(player.get_by_role('button', name='Play', exact=True)).to_be_visible()
    assert len(commands) == 1
    search = page.get_by_role('searchbox', name='Search your library')
    search.fill('two')
    search.press('Space')
    expect(search).to_have_value('two ')
    assert len(commands) == 1
    player.get_by_role('button', name='Play', exact=True).focus()
    page.keyboard.press('Space')
    expect(player.get_by_role('button', name='Pause', exact=True)).to_be_visible()
    assert len(commands) == 2
