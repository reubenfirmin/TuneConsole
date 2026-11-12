"""Space controls playback while respecting search and focused buttons."""
from playwright.sync_api import expect


def test_space_controls_playback_without_intercepting_search_or_focused_buttons(live_app, page, store):
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
    account = store.accounts.get_accounts()[0].id
    pid = store.playlists.upsert_playlist(account, 'PLKEYBOARD', 'Keyboard mix', 2, 'h', 1)
    tracks = [store.tracks.upsert_track(video, video, 'Artist', '', 180, 1)
              for video in ('keyboard', 'other')]
    store.playlists.set_playlist_tracks(pid, tracks)
    ratings = []

    def rate(route):
        ratings.append(route.request.post_data)
        if route.request.url.endswith('/now-playing/rate'):
            status['now_playing']['likeStatus'] = route.request.post_data_json['likeStatus']
            route.fulfill(json={'ok': True})
        else:
            route.fulfill(body='')

    page.route('**/track/like', rate)
    page.route('**/now-playing/rate', rate)
    page.goto(f'{live_app}/playlist/{pid}')
    for video in ('keyboard', 'other'):
        heart = page.locator(f'tr[data-vid="{video}"] .like-btn')
        for pressed in ('true', 'false'):
            with page.expect_request(lambda request: request.url.endswith(('/track/like', '/now-playing/rate'))):
                heart.click()
            expect(heart).to_have_attribute('aria-pressed', pressed)
            before = len(ratings)
            toggles = len(commands)
            with page.expect_request('**/now-playing/toggle*'):
                page.keyboard.press('Space')
            expect(player.get_by_role('button', name='Play' if status['now_playing']['paused'] else 'Pause', exact=True)).to_be_visible()
            assert len(commands) == toggles + 1
            assert len(ratings) == before
            expect(heart).to_have_attribute('aria-pressed', pressed)
        # Deliberate keyboard focus retains native Space/Enter rating activation.
        heart.focus()
        toggles = len(commands)
        for key, pressed in (('Space', 'true'), ('Enter', 'false')):
            with page.expect_request(lambda request: request.url.endswith(('/track/like', '/now-playing/rate'))):
                page.keyboard.press(key)
            expect(heart).to_have_attribute('aria-pressed', pressed)
            expect(heart).to_be_focused()
            assert len(commands) == toggles
