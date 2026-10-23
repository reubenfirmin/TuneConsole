"""Radio artwork motion, restored shape and playback history geometry."""
import pytest
from fastapi.templating import Jinja2Templates
from playwright.sync_api import expect
from tests.browser.radio_ui_support import radio_ui

def test_artwork_evolves_slowly_with_reduced_motion_and_pauses_when_hidden(page, radio_ui):
    api = radio_ui
    page.clock.install()
    page.emulate_media(reduced_motion='reduce')
    page.evaluate('''sessionStorage.setItem('radio-artwork', JSON.stringify({
      playlistId: 'PLRADIO', phase: 0, seed: 284,
    }))''')
    templates = Jinja2Templates(directory='src/yt_playlist/web/templates')
    api.playlist = templates.get_template('_partials/radio_playlist.html').render(session=api.ready, tracks=api.tracks)
    api.state = api.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    expect(page.locator('.radio-wave-landscape path')).to_have_count(14)
    page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).artworkRunning')
    flow = page.locator('.radio-junction-flow')
    expect(flow).to_have_css('animation-name', 'radio-stream-drift')
    expect(flow).to_have_css('animation-duration', '16s')
    expect(flow).to_have_css('animation-timing-function', 'linear')
    circulation = flow.evaluate('''el => {
      const length = el.getTotalLength();
      const samples = Array.from({length: 1000}, (_, i) => {
        const distance = (i + .5) / 1000 * length;
        const point = el.getPointAtLength(distance);
        return {x: point.x, y: point.y,
          dy: el.getPointAtLength(distance + .5).y - el.getPointAtLength(distance - .5).y};
      });
      const left = samples.find(p => p.x < 180 && p.y > 12 && p.y < 28);
      const right = samples.find(p => p.x > 820 && p.y > 12 && p.y < 28);
      const start = el.getPointAtLength(0), end = el.getPointAtLength(length);
      return {downLeft: left?.dy > 0, upRight: right?.dy < 0,
        closed: Math.hypot(start.x - end.x, start.y - end.y) < .01};
    }''')
    assert circulation == {'downLeft': True, 'upRight': True, 'closed': True}
    # The small dots have their own smooth clock, independent of the evolving
    # landscape. Four dash periods make the repeat visually seamless.
    offsets = flow.evaluate('''el => {
      const animation = el.getAnimations().find(a => a.animationName === 'radio-stream-drift');
      animation.pause();
      const offsets = [0, 1000, 15999, 16000].map(time => {
        animation.currentTime = time;
        return parseFloat(getComputedStyle(el).strokeDashoffset);
      });
      animation.play();
      return offsets;
    }''')
    assert -2 < offsets[1] - offsets[0] < -1.5
    assert abs(offsets[2] + 28) < .01
    assert abs(offsets[3] - offsets[0]) < .01
    expect(page.locator('#radio-landscape-0')).to_have_css('transition-duration', '0s')
    initial = page.locator('#radio-landscape-0').evaluate('el => getComputedStyle(el).d')
    phase = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    page.clock.run_for(20_000)
    elapsed = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') - phase
    assert .16 < elapsed < .19  # The fastest fold takes twelve minutes.
    expect(page.locator('#radio-landscape-0')).not_to_have_css('d', initial)
    expect(flow).not_to_have_css('stroke-dashoffset', '0px')

    bounds = page.evaluate('''() => {
      const model = Alpine.$data(document.querySelector('.radio-home'));
      const original = model.artworkPhase, layer = document.querySelector('.radio-wave-landscape');
      const probe = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      layer.append(probe);
      const boxes = [], shapes = new Map();
      for (const minute of [0, 1, 3, 6, 12, 30, 60, 180, 360, 720, 11.999, 12.001]) {
        model.artworkPhase = minute * Math.PI * 2 / 12;
        const samples = [];
        for (let line = 0; line < 14; line++) {
          probe.setAttribute('style', model.landscapeStyle(line) + '; transition: none !important');
          const box = probe.getBBox();
          boxes.push({x: box.x, y: box.y, width: box.width, height: box.height});
          const length = probe.getTotalLength();
          for (const fraction of [.2, .4, .6, .8]) {
            const point = probe.getPointAtLength(length * fraction);
            samples.push([point.x, point.y]);
          }
        }
        shapes.set(minute, samples);
      }
      const displacement = (a, b) => shapes.get(a).reduce((sum, point, i) =>
        sum + Math.hypot(point[0] - shapes.get(b)[i][0], point[1] - shapes.get(b)[i][1]), 0) / shapes.get(a).length;
      model.artworkPhase = original;
      probe.remove();
      // A session's seeded motion may return near its starting shape at one
      // particular instant. Require substantial evolution over the sampled
      // listening period rather than a specific old thirty-minute trajectory.
      const maximumChange = Math.max(...[1, 3, 6, 12, 30, 60, 180, 360, 720]
        .map(minute => displacement(0, minute)));
      return {boxes, maximumChange, boundaryStep: displacement(11.999, 12.001),
        width: model.drawingSpace.width,
        height: model.drawingSpace.historyHeight + model.drawingSpace.gapHeight};
    }''')
    assert bounds['maximumChange'] > bounds['width'] * .08
    assert bounds['boundaryStep'] < bounds['width'] * .001
    for box in bounds['boxes']:
        assert box['x'] < bounds['width'] and box['x'] + box['width'] > 0
        assert box['y'] < bounds['height'] and box['y'] + box['height'] > 0
        assert box['width'] > bounds['width'] * .9 and box['height'] > 20

    phase = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    for minute in (0, 30):
        page.evaluate('minute => { Alpine.$data(document.querySelector(".radio-home")).artworkPhase = minute * Math.PI * 2 / 12; }', minute)
        page.screenshot(path=f'/tmp/tuneconsole-radio-evolution-{minute}.png')
    page.evaluate('phase => { Alpine.$data(document.querySelector(".radio-home")).artworkPhase = phase; }', phase)

    page.get_by_role('tab').first.click()
    expect(page.locator('.radio-space-field')).to_be_hidden()
    expect(flow).to_have_css('animation-play-state', 'paused')
    phase = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    page.clock.run_for(30_000)
    assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') == phase
    page.get_by_role('tab', name='On air', exact=True).click()
    expect(page.locator('.radio-space-field')).to_be_visible()
    # A frame may run between the click and this check, but hidden time must
    # not be replayed or reset the drawing.
    resumed = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    assert phase <= resumed < phase + .01
    expect(flow).to_have_css('animation-play-state', 'running')
    page.clock.run_for(2000)
    assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') > phase
    page.emulate_media(reduced_motion='no-preference')
    page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).artworkRunning')
    phase = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    page.clock.run_for(30_000)
    assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') > phase

    page.evaluate('''() => {
      Object.defineProperty(document, 'hidden', {configurable: true, value: true});
      document.dispatchEvent(new Event('visibilitychange'));
    }''')
    expect(flow).to_have_css('animation-play-state', 'paused')
    phase = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    page.clock.fast_forward(30 * 60_000)
    assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') == phase
    resumed = page.evaluate('''() => {
      delete document.hidden;
      document.dispatchEvent(new Event('visibilitychange'));
      return Alpine.$data(document.querySelector('.radio-home')).artworkPhase;
    }''')
    assert resumed == phase
    expect(flow).to_have_css('animation-play-state', 'running')
    page.clock.run_for(250)
    change = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') - phase
    assert 0 < change < .003
    # A stalled visible frame also drops elapsed time instead of racing through it.
    phase += change
    page.clock.fast_forward(30 * 60_000)
    change = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') - phase
    assert 0 <= change < .003


def test_artwork_resumes_its_saved_shape_after_visiting_another_page(page, radio_ui):
    api = radio_ui
    templates = Jinja2Templates(directory='src/yt_playlist/web/templates')
    api.playlist = templates.get_template('_partials/radio_playlist.html').render(session=api.ready, tracks=api.tracks)
    api.state = api.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase = 15')
    page.get_by_role('link', name='Playlists', exact=True).click()
    saved = page.evaluate('JSON.parse(sessionStorage.getItem("radio-artwork"))')
    assert 15 <= saved['phase'] < 15.01
    page.get_by_role('link', name='Home', exact=True).click()
    page.get_by_role('tab', name='On air', exact=True).click()
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    resumed = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    assert saved['phase'] <= resumed < saved['phase'] + .01
    assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkSeed') == saved['seed']
    expect(page.locator('#radio-landscape-0')).to_have_css('transition-duration', '0s')


def test_record_cover_changes_immediately_when_replaying_history(page, radio_ui):
    api = radio_ui
    api.state = {**api.ready, 'position': 2, 'history_count': 3}
    templates = Jinja2Templates(directory='src/yt_playlist/web/templates')
    api.playlist = templates.get_template('_partials/radio_playlist.html').render(
        session=api.state, tracks=api.tracks)
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    cover = page.locator('.radio-record-cover')
    expect(cover).to_have_attribute('src', api.tracks[2]['thumbnail'])
    expect(cover).to_have_js_property('naturalWidth', 300)
    page.clock.install()
    requests = []
    page.route('**/play', lambda route: requests.append(route))
    page.locator('.radio-history .pl-play').first.click()
    expect(cover).to_have_attribute('src', api.tracks[0]['thumbnail'])
    assert len(requests) == 1
    requests[0].fulfill(json={'ok': True})
    # The history stays unchanged when native playback returns to an earlier song too.
    page.evaluate('''video => window.dispatchEvent(new CustomEvent('bridge-status', {detail: {
      connected: true, now_playing: {video_id: video, playlist_ytm_id: 'PLRADIO'}
    }}))''', api.tracks[0]['video_id'])
    expect(cover).to_have_attribute('src', api.tracks[0]['thumbnail'])
    page.evaluate('''video => window.dispatchEvent(new CustomEvent('bridge-status', {detail: {
      connected: true, now_playing: {video_id: video, playlist_ytm_id: 'PLRADIO'}
    }}))''', api.tracks[1]['video_id'])
    expect(cover).to_have_attribute('src', api.tracks[1]['thumbnail'])
    expect(page.locator('.radio-history tr')).to_have_count(3)


@pytest.mark.parametrize('width,height', [(1440, 1000), (1792, 1398), (390, 1000)])
def test_session_history_rises_from_junction_and_queues_preview_without_switching(page, radio_ui, width, height):
    api = radio_ui
    page.set_viewport_size({'width': width, 'height': height})
    api.ready = {**api.ready, 'directions': [
        {**branch, 'tracks': api.tracks[branch['slot'] + 1:branch['slot'] + 7]}
        for branch in api.ready['directions']]}
    templates = Jinja2Templates(directory='src/yt_playlist/web/templates')
    template = templates.get_template('_partials/radio_playlist.html')

    def playing(count):
        tracks = [{**track, 'album': 'Night album', 'album_browse': None,
                   'year': '2001', 'genre': 'ambient', 'duration': 230, 'liked': False}
                  for track in api.tracks]
        api.state = {**api.ready, 'tracks': tracks, 'history_count': count, 'position': count - 1,
                     'browser_age': 0, 'browser': {'player': {
                         'playlist_ytm_id': 'PLRADIO', 'video_id': tracks[count - 1]['video_id']}}}
        api.playlist = template.render(session=api.state, tracks=tracks)

    playing(1)
    session = page.locator('.radio-session')
    expect(session).to_be_visible(timeout=6000)
    rows = session.locator('.radio-history tr')
    expect(rows).to_have_count(1)
    expect(rows.last).to_have_attribute('aria-current', 'true')
    expect(session.get_by_role('columnheader', name='Year', exact=True)).to_have_count(1)
    expect(session.get_by_role('columnheader', name='Genre', exact=True)).to_have_count(1)
    expect(rows.last).to_contain_text('2001')
    expect(rows.last).to_contain_text('ambient')
    assert session.locator('.radio-empty-row').count() >= 2
    scroller = session.locator('.radio-track-scroll')
    assert abs(rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'] - (height / 2 - 16)) < 3
    expect(scroller).to_have_css('border-bottom-width', '0px')
    expect(session.locator('.radio-direction-label')).to_have_count(0)
    expect(session.locator('.radio-queue-art img')).to_have_count(12)
    drawing = session.locator('.radio-history-art')
    expect(drawing).to_be_visible()
    field = page.locator('body > .radio-space-field')
    expect(field).to_be_visible()
    assert field.bounding_box() == {'x': 0, 'y': 0, 'width': width, 'height': height}
    expect(field).to_have_css('pointer-events', 'none')
    assert drawing.bounding_box()['height'] > 80
    junction_box = session.locator('.radio-junction-space').bounding_box()
    assert abs(drawing.bounding_box()['y'] + drawing.bounding_box()['height'] - (junction_box['y'] + junction_box['height'])) < 3
    expect(session.locator('.track-table')).to_have_css('opacity', '1')
    assert '.85' in rows.first.evaluate('el => getComputedStyle(el).backgroundColor')
    expect(drawing.locator('.radio-record-cover')).to_have_attribute('src', api.tracks[0]['thumbnail'])
    expect(drawing.locator('.radio-record-cover')).to_have_js_property('naturalWidth', 300)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=f'/tmp/tuneconsole-radio-drawing-{width}.png')

    line = session.locator('.radio-junction-line')
    original = line.evaluate('el => getComputedStyle(el).d')
    other = session.locator('.radio-direction').nth(1)
    other.hover()
    expect(other).to_have_class('radio-direction is-preview')
    page.wait_for_function('(original) => getComputedStyle(document.querySelector(".radio-junction-line")).d !== original', arg=original)
    line.evaluate('el => Promise.all(el.getAnimations().map(animation => animation.finished))')
    expect(session.locator('.radio-direction.is-active')).to_have_attribute('data-slot', '0')
    assert session.locator('.radio-direction').nth(0).evaluate('el => getComputedStyle(el).borderColor') != other.evaluate('el => getComputedStyle(el).borderColor')
    page.screenshot(path=f'/tmp/tuneconsole-radio-session-{width}.png')
    page.mouse.move(0, 0)
    other.get_by_role('button').focus()
    expect(other).to_have_class('radio-direction is-preview')
    page.locator('.radio-menu > summary').focus()
    expect(other).not_to_have_class('radio-direction is-preview')
    page.emulate_media(reduced_motion='reduce')
    other.get_by_role('button').focus()
    expect(line).to_have_css('transition-duration', '0s')
    page.locator('.radio-menu > summary').focus()

    playing(3)
    expect(rows).to_have_count(3, timeout=6000)
    expect(rows.last).to_have_attribute('aria-current', 'true')
    assert abs(rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'] - (height / 2 - 16)) < 3
    expect(drawing.locator('.radio-record-cover')).to_have_attribute('src', api.tracks[2]['thumbnail'])
    playing(12)
    expect(rows).to_have_count(12, timeout=6000)
    expect(rows.last).to_have_attribute('aria-current', 'true')
    expect(session.locator('.radio-empty-row')).to_have_count(0)
    expect(drawing).to_be_visible()
    assert drawing.bounding_box()['height'] > 80
    assert scroller.evaluate('el => el.scrollTop') > 0
    assert abs(rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'] - (height / 2 - 16)) < 3
    # Polling must leave the user free to scroll back through earlier tracks.
    scroller.evaluate('el => el.scrollTop = 0')
    with page.expect_response('**/radio/status', timeout=6000):
        pass
    page.evaluate('() => new Promise(requestAnimationFrame)')
    assert scroller.evaluate('el => el.scrollTop') == 0
