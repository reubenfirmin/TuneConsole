"""The audible track remains visible in a table across page and row changes."""
import re

import pytest
from playwright.sync_api import expect

from tests.browser.test_playlist_browser import live_playlist_app

pytestmark = pytest.mark.browser


def test_playing_row_highlight_survives_navigation_and_status_changes(live_playlist_app, page):
    base, pid = live_playlist_app['base'], live_playlist_app['pid']
    status = {'connected': True, 'now_playing': {
        'video_id': 'v0', 'title': 'Song A', 'artist': 'Artist X', 'paused': False}}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': 1600, 'height': 1100})
    page.goto(f'{base}/playlist/{pid}')
    first, second = page.locator('tr[data-vid="v0"]'), page.locator('tr[data-vid="v1"]')
    expect(first).to_have_class(re.compile(r'\bnow-playing\b'))
    expect(second).not_to_have_class(re.compile(r'\bnow-playing\b'))
    assert first.locator('td').first.evaluate('cell => getComputedStyle(cell).boxShadow') != 'none'
    assert first.locator('td').first.evaluate('cell => getComputedStyle(cell).backgroundColor') != 'rgba(0, 0, 0, 0)'
    page.screenshot(path='/tmp/tuneconsole-870-highlight.png', full_page=True)
    status['now_playing'] = {'video_id': 'v1', 'title': 'Song B', 'artist': 'Artist Y', 'paused': True}
    page.evaluate('window.playbackStatus.refresh()')
    expect(second).to_have_class(re.compile(r'\bnow-playing\b'))
    expect(first).not_to_have_class(re.compile(r'\bnow-playing\b'))
    page.get_by_role('link', name='Playlists', exact=True).click()
    page.get_by_role('link', name='Mix', exact=True).click()
    expect(second).to_have_class(re.compile(r'\bnow-playing\b'))
    status['connected'] = False
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('tr.now-playing')).to_have_count(0)
    assert not errors


def test_alternate_video_falls_back_to_title_and_artist_without_matching_other_versions(live_playlist_app, page):
    base, pid = live_playlist_app['base'], live_playlist_app['pid']
    status = {'connected': True, 'now_playing': {
        'video_id': 'alternate-video', 'title': 'Song A', 'artist': 'Artist X', 'paused': False}}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.goto(f'{base}/playlist/{pid}')
    expect(page.locator('tr[data-vid="v0"]')).to_have_class(re.compile(r'\bnow-playing\b'))
    for title, artist in [('Song A', 'Another artist'), ('Song A (Live)', 'Artist X'), ('', '')]:
        status['now_playing'].update(title=title, artist=artist)
        page.evaluate('window.playbackStatus.refresh()')
        expect(page.locator('tr.now-playing')).to_have_count(0)
    # An exact video match wins over inconsistent sensor metadata.
    status['now_playing'].update(video_id='v1', title='Song A', artist='Artist X')
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('tr[data-vid="v1"]')).to_have_class(re.compile(r'\bnow-playing\b'))
    expect(page.locator('tr[data-vid="v0"]')).not_to_have_class(re.compile(r'\bnow-playing\b'))
