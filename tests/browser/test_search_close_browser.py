"""Closing search must not change later focus or cancel a reopened search."""

import pytest
from playwright.sync_api import expect


pytestmark = pytest.mark.browser


def observe_close(page):
    page.evaluate("""() => {
        window.searchClosed = false;
        document.querySelector('#music-search').addEventListener('close', () => {
            window.searchClosed = true;
        }, {once: true});
    }""")


@pytest.mark.parametrize('focus_before_close_event', [False, True])
def test_player_focus_survives_search_close(live_app, page, focus_before_close_event):
    status = {'connected': True, 'now_playing': {
        'video_id': 'keyboard', 'title': 'Song', 'artist': 'Artist', 'paused': True}}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    commands = []

    def toggle(route):
        commands.append(route.request.url)
        status['now_playing']['paused'] = False
        route.fulfill(json={'ok': True})

    page.route('**/now-playing/toggle*', toggle)
    page.goto(live_app + '/artists')
    player = page.locator('.global-now-playing')
    play = player.get_by_role('button', name='Play', exact=True)
    expect(play).to_be_visible()
    page.get_by_role('button', name='Search music…', exact=True).click()
    expect(page.get_by_role('searchbox', name='Search music', exact=True)).to_be_focused()
    observe_close(page)
    if focus_before_close_event:
        assert page.evaluate("""() => {
            document.querySelector('[aria-label="Close music search"]').click();
            const play = document.querySelector('.global-now-playing button[aria-label="Play"]');
            play.focus();
            return document.activeElement === play && !window.searchClosed;
        }""")
    else:
        page.get_by_role('button', name='Close music search', exact=True).click()
    page.wait_for_function('window.searchClosed')
    if not focus_before_close_event:
        play.focus()
    expect(play).to_be_focused()
    page.keyboard.press('Space')
    expect(player.get_by_role('button', name='Pause', exact=True)).to_be_visible()
    expect(page.locator('#music-search')).not_to_be_visible()
    assert len(commands) == 1


@pytest.mark.parametrize('reopen_before_close_event', [False, True])
def test_reopened_search_survives_old_close(live_app, page, reopen_before_close_event):
    hold_until_close = False

    def catalog(route):
        if hold_until_close:
            page.wait_for_function('window.searchClosed')
        route.fulfill(json={'songs': [{'video_id': 'reopened-result',
            'title': 'The reopened result', 'artist': 'Artist', 'can_add': False}]})

    page.route('**/search/catalog?*', catalog)
    page.route('**/search/catalog/sections?*', lambda route: route.fulfill(json={
        'artists': [], 'albums': [], 'playlists': []}))
    page.route('**/search/omni?*', lambda route: route.fulfill(body=''))
    page.goto(live_app + '/artists')
    opener = page.get_by_role('button', name='Search music…', exact=True)
    opener.click()
    dialog = page.locator('#music-search')
    dialog.get_by_role('tab', name='YouTube Music', exact=True).click()
    field = dialog.get_by_role('searchbox', name='Search music', exact=True)
    field.fill('reopened')
    result = dialog.locator('.music-search-song-copy').get_by_text('The reopened result', exact=True)
    expect(result).to_be_visible()
    observe_close(page)
    hold_until_close = True
    if reopen_before_close_event:
        assert opener.evaluate("""opener => {
            document.querySelector('[aria-label="Close music search"]').click();
            opener.click();
            return document.querySelector('#music-search').open && !window.searchClosed;
        }""")
    else:
        dialog.get_by_role('button', name='Close music search', exact=True).click()
        page.wait_for_function('window.searchClosed')
        opener.click()
    page.wait_for_function('window.searchClosed')
    expect(dialog).to_be_visible()
    expect(field).to_have_value('reopened')
    expect(field).to_be_focused()
    expect(result).to_be_visible()
    expect(dialog.get_by_role('button', name='Search YouTube Music again', exact=True)).not_to_be_visible()


@pytest.mark.parametrize('control', ['button', 'Escape'])
def test_search_close_restores_opener(live_app, page, control):
    page.goto(live_app + '/artists')
    opener = page.get_by_role('button', name='Search music…', exact=True)
    opener.click()
    dialog = page.locator('#music-search')
    expect(dialog.get_by_role('searchbox', name='Search music', exact=True)).to_be_focused()
    observe_close(page)
    if control == 'button':
        dialog.get_by_role('button', name='Close music search', exact=True).click()
    else:
        page.keyboard.press(control)
    page.wait_for_function('window.searchClosed')
    expect(dialog).not_to_be_visible()
    expect(opener).to_be_focused()
