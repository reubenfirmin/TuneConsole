"""The first hidden video is inspected without disturbing the visible player or strip."""
import pytest
from PIL import Image
from playwright.sync_api import expect

from tests.browser.test_tv_browser import tv_ui
from tests.browser.test_tv_stream_browser import MODEL, publish_pool
from tests.conftest import FakeClient
from tests.test_tv_visual import frame, sheet, storyboard
from yt_playlist.rec import tv_visual


def test_only_two_ahead_is_sampled_and_static_replacements_stay_hidden(page, live_app, tv_ui, store, monkeypatch):
    publish_pool(store, live_app, 7)
    checked = []
    def previews(self, video_id):
        checked.append(video_id)
        return storyboard(video_id)
    monkeypatch.setattr(FakeClient, 'get_video_storyboard', previews, raising=False)
    def read(url, deadline):
        if '/visual00002/' in url:
            return sheet()
        return sheet(frame().transpose(Image.Transpose.FLIP_LEFT_RIGHT))
    monkeypatch.setattr(tv_visual, '_read_image', read)
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing && !{MODEL}.items.some(item => item.video_id === "visual00002")')
    expect(page.locator('.tv-filmstrip button')).to_have_count(2)
    assert page.evaluate(f'{MODEL}.items.slice(0, 3).map(item => item.video_id)') == [
        'visual00000', 'visual00001', 'visual00003']
    page.wait_for_function(f'{MODEL}.items[2].video_id === "visual00003"')
    assert page.evaluate('window.tvPlayer.id') == 'visual00000'
    assert page.evaluate('window.tvCalls') == [['play', 'visual00000']]
    expect(page.locator('.tv-filmstrip button').nth(1)).to_have_attribute('aria-label', 'Up next: Video 1')
    # Wait for the replacement's completed check before advancing.
    page.wait_for_function("async () => (await (await fetch('/home/tv/playlist/' + "
        + MODEL + ".playlistId + '/visual-check', {method:'POST', body:new URLSearchParams({video_id:'visual00003'})})).json()).verdict === 'moving'")
    assert checked == ['visual00002', 'visual00003']
    assert tv_visual.static_ids(store) == {'visual00002'}
    assert all(event['video_id'] != 'visual00002' for event in store.history.play_events_since(0))
    page.screenshot(path='/tmp/tuneconsole-439-hidden-visual-check.png')
    page.evaluate('window.tvPlayer.options.events.onStateChange({data:0,target:window.tvPlayer})')
    page.wait_for_function(f'{MODEL}.current.video_id === "visual00001" && {MODEL}.playing')
    expect(page.locator('.tv-filmstrip button')).to_have_count(3)
    expect(page.locator('.tv-filmstrip button').nth(2)).to_have_attribute('aria-label', 'Up next: Video 3')


@pytest.mark.parametrize('change', ['advance', 'release', 'reset'])
def test_late_static_verdict_cannot_change_a_newer_queue_or_revealed_thumbnail(page, live_app, tv_ui, store, change):
    publish_pool(store, live_app, 7)
    pending = []
    page.route('**/visual-check', lambda route: pending.append(route))
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    page.wait_for_timeout(50)
    assert len(pending) == 1 and 'video_id=visual00002' in pending[0].request.post_data
    if change == 'advance':
        page.evaluate('window.tvPlayer.options.events.onStateChange({data:0,target:window.tvPlayer})')
        page.wait_for_function(f'{MODEL}.current.video_id === "visual00001"')
    elif change == 'release':
        page.evaluate('window.tvPlaybackControls.release()')
    elif change == 'reset':
        page.evaluate(f'{MODEL}.stopPlayer(); {MODEL}.items = []')
    before = page.evaluate(f'{MODEL}.items.map(item => item.queue_id)')
    pending.pop(0).fulfill(json={'video_id': 'visual00002', 'verdict': 'static'})
    page.wait_for_timeout(100)
    assert page.evaluate(f'{MODEL}.items.map(item => item.queue_id)') == before
    if change == 'advance':
        expect(page.get_by_role('button', name='Up next: Video 2', exact=True)).to_be_visible()
    elif change == 'release':
        assert not page.evaluate(f'{MODEL}.playing || window.tvPlaybackControls.active()')


def test_static_screening_continues_when_tv_is_in_the_background(page, live_app, tv_ui, store):
    publish_pool(store, live_app, 7)
    pending = []
    page.route('**/visual-check', lambda route: pending.append(route))
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    page.wait_for_timeout(50)
    assert pending and 'video_id=visual00002' in pending[0].request.post_data
    page.get_by_role('tab', name='Mix tapes', exact=True).click()
    pending.pop(0).fulfill(json={'video_id': 'visual00002', 'verdict': 'static'})
    page.wait_for_function(f'!{MODEL}.items.some(item => item.video_id === "visual00002")')
    assert page.evaluate(f'{MODEL}.playing && window.tvPlaybackControls.active()')
    assert page.evaluate('window.tvPlayer.id') == 'visual00000'
    page.get_by_role('tab', name='TV', exact=True).click()
    expect(page.locator('.tv-filmstrip button')).to_have_count(2)
    expect(page.locator('.tv-backdrop')).to_be_visible()
