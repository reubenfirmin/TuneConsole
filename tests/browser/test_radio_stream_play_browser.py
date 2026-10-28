"""Stream artwork plays a specific song; artist and song links navigate independently."""
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.browser.radio_ui_support import radio_ui
from tests.template_support import web_templates


def show_session(page, api):
    for index, track in enumerate(api.tracks):
        track.update(album=f'Album {index + 1}', album_browse_id=f'MPRE{index}')
    api.ready['menu_id'] = 'visible-streams'
    api.playlist = web_templates().get_template('_partials/radio_playlist.html').render(
        session=api.ready, tracks=api.tracks)
    api.state = api.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)


@pytest.mark.parametrize('width', [1440, 390])
def test_stream_artwork_hover_keyboard_play_and_metadata_links(page, radio_ui, tmp_path, width):
    api = radio_ui
    page.set_viewport_size({'width': width, 'height': 1000})
    show_session(page, api)
    selections = []

    def select(route):
        selections.append(route.request.post_data_json)
        route.fulfill(json={'ok': True, **api.state})

    page.route('**/radio/select', select)
    branch = page.locator('.radio-direction[data-slot="0"]')
    row = branch.locator('.radio-proposed-track').nth(2)
    button = row.get_by_role('button', name='Play Night frequency 4 by Artist 4 in stream 1', exact=True)
    button.scroll_into_view_if_needed()
    button.hover()
    expect(button.locator('.radio-proposed-play')).to_have_css('opacity', '1')
    icon = button.locator('.radio-proposed-play svg')
    assert icon.bounding_box()['width'] >= 24
    expect(icon).to_have_css('stroke', 'none')
    song_url = urlsplit(row.locator('.radio-proposed-song').get_attribute('href'))
    assert song_url.path == '/track/radio3'
    assert parse_qs(song_url.query)['title'] == ['Night frequency 4']
    expect(row.locator('.radio-proposed-artist')).to_have_attribute('href', '/artist?name=Artist%204')
    expect(row.locator('.radio-proposed-info > a')).to_have_count(2)
    expect(row.locator('.radio-proposed-info')).to_have_text(re.compile(r'^\s*Night frequency 4\s*Artist 4\s*$'))
    expect(page.locator('.radio-direction .radio-proposed-album')).to_have_count(0)
    page.locator('.radio-session').screenshot(path=tmp_path / f'stream-song-play-{width}.png')
    page.mouse.move(0, 0)
    button.focus()
    expect(button.locator('.radio-proposed-play')).to_have_css('opacity', '1')
    with page.expect_request('**/radio/select'):
        button.press('Enter')
    assert selections == [{'proposal_id': 'direction0', 'menu_id': 'visible-streams', 'video_id': 'radio3'}]
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_rapid_song_clicks_keep_latest_choice_and_explain_removed_song_inline(page, radio_ui):
    api = radio_ui
    show_session(page, api)
    pending = []

    def select(route):
        pending.append(route)
        if len(pending) == 2:
            api.state = {**api.state, 'directions': [
                {**branch, 'tracks': [song for song in branch['tracks'] if song['video_id'] != 'radio5']}
                if branch['slot'] == 2 else branch for branch in api.state['directions']]}
            route.fulfill(json={'ok': False, 'stale': True,
                'reason': 'That song left the stream. Choose a current song.'})

    page.route('**/radio/select', select)
    first = page.locator('.radio-direction[data-slot="1"] .radio-proposed-art').nth(1)
    latest = page.locator('.radio-direction[data-slot="2"] .radio-proposed-art').nth(2)
    with page.expect_request('**/radio/select'):
        first.click()
    latest.click()
    assert len(pending) == 1
    assert pending[0].request.post_data_json['video_id'] == 'radio3'
    with page.expect_response(lambda response: response.url.endswith('/radio/select')
                             and response.request.post_data_json.get('video_id') == 'radio5'):
        pending[0].fulfill(json={'ok': True, **api.state})
    assert len(pending) == 2
    assert pending[1].request.post_data_json == {
        'proposal_id': 'direction2', 'menu_id': 'visible-streams', 'video_id': 'radio5'}
    message = page.locator('.radio-direction[data-slot="2"] .radio-selection-error')
    expect(message).to_contain_text('That song is no longer in this stream')
    expect(message).to_have_css('position', 'static')
    expect(page.locator('.radio-proposed-track.is-pending')).to_have_count(0)
    expect(page.locator('.radio-proposed-track.is-skipping')).to_have_count(0)
    expect(page.locator('.radio-song-pending:visible')).to_have_count(0)
    expect(page.locator('.radio-session')).to_be_visible()


@pytest.mark.parametrize('width,confirmation', [(1440, 'bridge'), (390, 'observer')])
def test_pending_song_animates_and_grays_only_preceding_songs_until_playback(
        page, radio_ui, width, confirmation):
    api = radio_ui
    api.ready['directions'][0]['tracks'] = api.tracks[1:7]
    api.ready['selection_error'] = 'An earlier choice failed.'
    page.set_viewport_size({'width': width, 'height': 1000})
    page.emulate_media(reduced_motion='reduce')
    show_session(page, api)
    requests = []
    page.route('**/radio/select', lambda route: requests.append(route))
    branch = page.locator('.radio-direction[data-slot="0"]')
    rows = branch.locator('.radio-proposed-track')
    target = rows.nth(3)
    button = target.locator('.radio-proposed-art')
    with page.expect_request('**/radio/select'):
        button.click()

    status = branch.locator('.radio-song-pending')
    expect(status).to_be_visible()
    expect(status).to_contain_text('Night frequency 5')
    expect(status).to_contain_text('Starting · Artist 5')
    page.evaluate('Alpine.$data(document.querySelector(".radio-home")).refresh()')
    expect(page.locator('.radio-selection-error:visible')).to_have_count(0)
    expect(status).to_be_visible()
    expect(button).to_have_attribute('aria-busy', 'true')
    expect(target).to_have_class('radio-proposed-track is-pending')
    expect(branch.locator('.is-skipping')).to_have_count(3)
    for index in range(3):
        expect(rows.nth(index)).to_have_css('opacity', '0.35')
        expect(rows.nth(index)).to_have_css('filter', 'grayscale(1)')
        expect(rows.nth(index)).to_have_attribute('aria-description', 'Will be skipped when Night frequency 5 starts')
    expect(rows.nth(4)).to_have_css('opacity', '1')
    expect(page.locator('.radio-direction:not([data-slot="0"]) .is-skipping')).to_have_count(0)
    expect(branch.locator('.radio-saved-dot:visible')).to_have_count(0)
    spinner = target.locator('.radio-song-spinner')
    expect(spinner).to_have_css('animation-play-state', 'running')
    animation_time = spinner.evaluate('el => el.getAnimations()[0].currentTime')
    page.wait_for_function('start => document.querySelector(".is-pending .radio-song-spinner").getAnimations()[0].currentTime > start + 100',
                           arg=animation_time)
    page.locator('.radio-session').screenshot(path=f'/tmp/tuneconsole-radio-song-pending-{width}.png')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

    # An accepted request and a settled playlist do not establish playback.
    api.state = {**api.state, 'selection_error': None}
    requests[0].fulfill(json={'ok': True, **api.state})
    page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).pendingSong?.accepted === true')
    expect(status).to_be_visible()
    api.state = {**api.state, 'browser_age': 20, 'browser': {'player': {
        'playlist_ytm_id': 'PLRADIO', 'video_id': 'radio4', 'paused': False}}}
    page.evaluate('Alpine.$data(document.querySelector(".radio-home")).refresh()')
    expect(status).to_be_visible()
    api.state = {**api.state, 'browser_age': 0, 'browser': {'player': {
        'playlist_ytm_id': 'PLRADIO', 'video_id': 'radio4', 'paused': True}}}
    page.evaluate('Alpine.$data(document.querySelector(".radio-home")).refresh()')
    expect(status).to_be_visible()

    # The header retains the requested title even if a queue refresh removes it.
    api.state = {**api.state, 'directions': [
        {**branch, 'tracks': branch['tracks'][4:]} if branch['slot'] == 0 else branch
        for branch in api.state['directions']]}
    page.evaluate('Alpine.$data(document.querySelector(".radio-home")).refresh()')
    expect(branch.locator('.is-pending')).to_have_count(1)
    expect(branch.locator('.is-skipping')).to_have_count(3)
    expect(branch.locator('.is-pending .radio-proposed-art')).to_be_disabled()
    expect(status).to_contain_text('Night frequency 5')
    for playing in [
        {'playlist_ytm_id': 'PLOTHER', 'video_id': 'radio4', 'paused': False},
        {'playlist_ytm_id': 'PLRADIO', 'video_id': 'radio4', 'paused': True},
        {'playlist_ytm_id': 'PLRADIO', 'video_id': 'radio0', 'paused': False},
    ]:
        page.evaluate('song => window.dispatchEvent(new CustomEvent("bridge-status", {detail: {connected: true, now_playing: song}}))', playing)
        expect(status).to_be_visible()
    playing = {'playlist_ytm_id': 'PLRADIO', 'video_id': 'radio4', 'paused': False}
    if confirmation == 'bridge':
        page.evaluate('song => window.dispatchEvent(new CustomEvent("bridge-status", {detail: {connected: true, now_playing: song}}))', playing)
    else:
        api.state = {**api.state, 'browser_age': 0, 'browser': {'player': playing}}
        page.evaluate('Alpine.$data(document.querySelector(".radio-home")).refresh()')
    expect(status).to_be_hidden()
    expect(page.locator('.is-skipping')).to_have_count(0)


def test_pending_feedback_follows_latest_click_and_ignores_older_acknowledgement(page, radio_ui):
    api = radio_ui
    show_session(page, api)
    requests = []

    def select(route):
        requests.append(route)
        if len(requests) == 2:
            route.fulfill(json={'ok': True, **api.state})

    page.route('**/radio/select', select)
    first_branch = page.locator('.radio-direction[data-slot="1"]')
    latest_branch = page.locator('.radio-direction[data-slot="2"]')
    with page.expect_request('**/radio/select'):
        first_branch.locator('.radio-proposed-art').nth(1).click()
    latest_branch.locator('.radio-proposed-art').nth(2).click()
    expect(first_branch.locator('.radio-song-pending')).to_be_hidden()
    expect(first_branch.locator('.is-skipping')).to_have_count(0)
    expect(latest_branch.locator('.radio-song-pending')).to_contain_text('Night frequency 6')
    expect(latest_branch.locator('.is-skipping')).to_have_count(2)
    with page.expect_response(lambda response: response.url.endswith('/radio/select')
                             and response.request.post_data_json.get('video_id') == 'radio5'):
        requests[0].fulfill(json={'ok': True, **api.state})
    page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).pendingSong?.accepted === true')
    page.evaluate('''() => window.dispatchEvent(new CustomEvent('bridge-status', {detail: {
        connected: true, now_playing: {playlist_ytm_id: 'PLRADIO', video_id: 'radio3', paused: false}}}))''')
    expect(latest_branch.locator('.radio-song-pending')).to_be_visible()
    expect(latest_branch.locator('.is-pending')).to_have_count(1)
    expect(latest_branch.locator('.is-skipping')).to_have_count(2)


@pytest.mark.parametrize('kind', ['song', 'direction'])
def test_stale_click_refreshes_and_retries_same_selection_without_error(page, radio_ui, kind):
    api = radio_ui
    show_session(page, api)
    selections = []

    def select(route):
        selections.append(route.request.post_data_json)
        if len(selections) == 1:
            branch = api.state['directions'][1]
            fresh = {**branch, 'tracks': api.tracks[3:6] if kind == 'song' else branch['tracks'],
                     'id': 'fresh-direction1' if kind == 'song' else branch['id']}
            api.state = {**api.state, 'menu_id': 'refreshed-menu',
                         'directions': [fresh if item['slot'] == 1 else item for item in api.state['directions']]}
            route.fulfill(json={'ok': False, 'stale': True,
                                'reason': 'The next songs changed. Choose a direction again.'})
        else:
            route.fulfill(json={'ok': True, **api.state})

    page.route('**/radio/select', select)
    branch = page.locator('.radio-direction[data-slot="1"]')
    with page.expect_response(lambda response: response.url.endswith('/radio/select')
                             and response.request.post_data_json.get('menu_id') == 'refreshed-menu'):
        if kind == 'song':
            branch.locator('.radio-proposed-art').nth(2).click()
        else:
            branch.locator('.radio-direction-heading').click()
    assert len(selections) == 2
    expected = {'proposal_id': 'fresh-direction1' if kind == 'song' else 'direction1',
                'menu_id': 'refreshed-menu', **({'video_id': 'radio4'} if kind == 'song' else {})}
    assert selections[1] == expected
    expect(page.locator('.radio-selection-error:visible')).to_have_count(0)
    if kind == 'song':
        expect(branch.locator('.radio-song-pending')).to_contain_text('Night frequency 5')
        expect(branch.locator('.is-pending')).to_have_attribute('data-video-id', 'radio4')
        # A refreshed proposal removed radio2. Its gray preview stays until playback.
        expect(branch.locator('.is-skipping')).to_have_count(2)
        assert branch.locator('.is-skipping').evaluate_all('rows => rows.map(row => row.dataset.videoId)') == ['radio2', 'radio3']
        expect(branch.locator('[data-video-id="radio2"] .radio-proposed-art')).to_be_disabled()
        expect(branch.locator('[data-video-id="radio3"] .radio-proposed-art')).to_be_enabled()
        expect(branch.locator('[data-video-id="radio5"]')).not_to_have_class(re.compile('is-skipping'))
        assert branch.locator('.radio-direction-tracks').evaluate('el => el.getAnimations().length') == 0
        page.locator('.radio-session').screenshot(path='/tmp/tuneconsole-radio-stale-recovered.png')


@pytest.mark.parametrize('changed', ['moved-song', 'session', 'continually-stale'])
def test_stale_retry_is_bounded_and_never_changes_song_stream_or_session(page, radio_ui, changed):
    api = radio_ui
    show_session(page, api)
    selections = []

    def select(route):
        selections.append(route.request.post_data_json)
        if changed == 'session':
            api.state = {**api.state, 'playlist_id': 'PLNEWSESSION'}
        elif changed == 'moved-song':
            api.state = {**api.state, 'directions': [
                {**branch, 'tracks': [song for song in branch['tracks'] if song['video_id'] != 'radio4']}
                if branch['slot'] == 1 else branch for branch in api.state['directions']]}
        route.fulfill(json={'ok': False, 'stale': True,
                            'reason': 'The next songs changed. Choose a direction again.'})

    page.route('**/radio/select', select)
    page.locator('.radio-direction[data-slot="1"] .radio-proposed-art').nth(2).click()
    if changed == 'session':
        page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).state.playlist_id === "PLNEWSESSION"')
        expect(page.locator('.radio-selection-error:visible')).to_have_count(0)
    else:
        error = page.locator('.radio-direction[data-slot="1"] .radio-selection-error')
        expect(error).to_be_visible()
        expect(error).to_have_css('position', 'static')
        expect(error).not_to_contain_text('The next songs changed')
    assert len(selections) == (3 if changed == 'continually-stale' else 1)
    assert all(selection.get('video_id') == 'radio4' for selection in selections)
    expect(page.locator('.radio-song-pending:visible')).to_have_count(0)
    expect(page.locator('.is-skipping')).to_have_count(0)


def test_latest_click_supersedes_a_stale_refresh_before_it_can_retry(page, radio_ui):
    api = radio_ui
    show_session(page, api)
    selections, refreshes = [], []

    def status(route):
        refreshes.append(route)
        if len(refreshes) > 1:
            route.fulfill(json=api.state)

    def select(route):
        selections.append(route.request.post_data_json)
        route.fulfill(json={'ok': False, 'stale': True} if len(selections) == 1 else {'ok': True, **api.state})

    page.route('**/radio/status', status)
    page.route('**/radio/select', select)
    with page.expect_request('**/radio/status'):
        page.locator('.radio-direction[data-slot="1"] .radio-proposed-art').nth(2).click()
    latest = page.locator('.radio-direction[data-slot="2"]')
    latest.locator('.radio-proposed-art').nth(2).click()
    with page.expect_response(lambda response: response.url.endswith('/radio/select')
                             and response.request.post_data_json.get('video_id') == 'radio5'):
        refreshes[0].fulfill(json=api.state)
    assert [selection.get('video_id') for selection in selections] == ['radio4', 'radio5']
    expect(latest.locator('.radio-song-pending')).to_contain_text('Night frequency 6')
    expect(latest.locator('.is-skipping')).to_have_count(2)
    expect(page.locator('.radio-selection-error:visible')).to_have_count(0)


@pytest.mark.parametrize('failure', ['network', 'server', 'stopped', 'direction'])
def test_pending_song_and_skip_feedback_clear_on_failure_or_session_change(page, radio_ui, failure):
    api = radio_ui
    show_session(page, api)
    requests = []
    page.route('**/radio/select', lambda route: requests.append(route))
    with page.expect_request('**/radio/select'):
        page.locator('.radio-direction[data-slot="0"] .radio-proposed-art').nth(2).click()
    expect(page.locator('.is-skipping')).to_have_count(2)
    if failure == 'network':
        requests[0].abort()
        expect(page.locator('.radio-selection-error:visible')).to_have_count(1)
        assert len(requests) == 1, 'An uncertain request must never be retried'
    else:
        requests[0].fulfill(json={'ok': True, **api.state})
        page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).pendingSong?.accepted === true')
        if failure == 'server':
            api.state = {**api.state, 'selection_error': 'That song could not be added. Choose another song.'}
        elif failure == 'stopped':
            api.state = {'active': False}
        else:
            with page.expect_request('**/radio/select'):
                page.locator('.radio-direction[data-slot="1"] .radio-direction-heading').click()
            requests[1].fulfill(json={'ok': True, **api.state})
        page.evaluate('Alpine.$data(document.querySelector(".radio-home")).refresh()')
    expect(page.locator('.is-pending')).to_have_count(0)
    expect(page.locator('.is-skipping')).to_have_count(0)
    expect(page.locator('.radio-song-pending:visible')).to_have_count(0)


def test_song_play_requires_loaded_server_support_and_recovers_after_restart(page, radio_ui):
    api = radio_ui
    api.ready.pop('can_play_songs')
    show_session(page, api)
    selections = []

    # The older server accepts this request but ignores video_id. Never send it
    # until a fresh status response establishes support for explicit playback.
    def legacy_select(route):
        selections.append(route.request.post_data_json)
        route.fulfill(json={'ok': True, **api.state})

    page.route('**/radio/select', legacy_select)
    button = page.locator('.radio-direction[data-slot="1"] .radio-proposed-art').nth(1)
    button.click()
    expect(page.locator('.radio-selection-error:visible')).to_contain_text('Restart TuneConsole')
    assert selections == []
    expect(page.locator('.radio-direction[data-slot="1"] .radio-direction-heading')).to_have_attribute(
        'aria-pressed', 'false')

    api.state = {**api.state, 'can_play_songs': True}
    page.wait_for_function("Alpine.$data(document.querySelector('.radio-home')).state.can_play_songs === true")
    with page.expect_request('**/radio/select'):
        button.click()
    assert selections == [{'proposal_id': 'direction1', 'menu_id': 'visible-streams', 'video_id': 'radio3'}]
    expect(page.locator('.radio-selection-error:visible')).to_have_count(0)


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('kind,expected', [('artist', '/artist?name=Artist%202'), ('song', '/track/radio1')])
def test_stream_metadata_link_opens_its_page_without_selecting_playback(page, radio_ui, width, kind, expected):
    api = radio_ui
    page.set_viewport_size({'width': width, 'height': 1000})
    show_session(page, api)
    selections = []
    page.route('**/radio/select', lambda route: (selections.append(route.request.post_data_json), route.fulfill(json={'ok': False})))
    link = page.locator(f'.radio-direction[data-slot="0"] .radio-proposed-{kind}').first
    href = link.get_attribute('href')
    if kind == 'song':
        song_url = urlsplit(href)
        assert song_url.path == expected
        assert parse_qs(song_url.query)['title'] == ['Night frequency 2']
        assert parse_qs(song_url.query)['artist'] == ['Artist 2']
    else:
        assert href == expected
    page.route('**' + href, lambda route: route.fulfill(content_type='text/html', body=f'<h1>{kind} details</h1>'))
    link.click()
    expect(page).to_have_url(re.compile(re.escape(href) + '$'))
    expect(page.locator('h1')).to_have_text(f'{kind} details')
    assert selections == []
