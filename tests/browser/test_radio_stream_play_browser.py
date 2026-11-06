"""Stream artwork plays a specific song; artist and song links navigate independently."""
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError, expect

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


@pytest.mark.parametrize('width', [1440])
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














@pytest.mark.parametrize('failure', ['http'])
def test_unconfirmed_selection_reconciles_without_diagnostics_or_replay(page, radio_ui, failure):
    api = radio_ui
    show_session(page, api)
    requests = []
    page.evaluate('window.selectionDocument = document')

    def select(route):
        requests.append(route)
        if failure == 'http':
            route.fulfill(status=503, body='database timeout: internal-diagnostic-879')
        elif failure == 'rejected':
            route.fulfill(json={'ok': False, 'reason': 'socket failure: internal-diagnostic-879'})
        elif failure == 'invalid-body':
            route.fulfill(content_type='application/json', body='internal-diagnostic-879')
        # The stalled response is deliberately left pending until the request
        # deadline releases the control. A later response cannot confirm it.

    page.route('**/radio/select', select)
    page.locator('.radio-direction[data-slot="1"] .radio-proposed-art').nth(2).click()
    message = page.locator('.radio-direction[data-slot="1"] .radio-selection-error')
    expect(message).to_contain_text('Playback not confirmed', timeout=11000)
    page.wait_for_function('!Alpine.$data(document.querySelector(".radio-home")).switchSending')
    expect(page.locator('.is-pending, .is-skipping')).to_have_count(0)
    expect(page.locator('body')).not_to_contain_text('internal-diagnostic-879')
    assert page.evaluate('document === window.selectionDocument')
    saved = page.evaluate('JSON.parse(sessionStorage.getItem("radio-selection"))')
    assert saved['playlistId'] == 'PLRADIO' and saved['video_id'] == 'radio4' and saved['slot'] == 1
    assert len(requests) == 1
    if failure == 'stalled':
        requests[0].fulfill(json={'ok': True, **api.state})

    page.reload()
    expect(message).to_contain_text('Playback not confirmed')
    assert len(requests) == 1, 'Reload must only reconcile, never repeat an uncertain selection'
    api.state = {**api.state, 'browser_age': 0, 'browser': {'player': {
        'playlist_ytm_id': 'PLRADIO', 'video_id': 'radio4', 'paused': False}}}
    page.evaluate('Alpine.$data(document.querySelector(".radio-home")).refresh()')
    expect(page.locator('.radio-selection-error:visible')).to_have_count(0)
    assert page.evaluate('sessionStorage.getItem("radio-selection")') is None
    assert len(requests) == 1
