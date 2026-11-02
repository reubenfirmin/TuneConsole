"""The composed starting landscape stays ordered at desktop sizes."""
from io import BytesIO

import pytest
from PIL import Image
from tests.template_support import web_templates
from playwright.sync_api import expect

from tests.browser.radio_ui_support import radio_ui  # noqa: F401


def _assert_loaded_record_pixels(page, url, color):
    cover = page.locator('.radio-record-cover')
    # This attribute is set by the native SVG load event, not the preloader.
    expect(cover).to_have_attribute('data-loaded', url)
    expect(cover).to_be_visible()
    box = cover.bounding_box()
    copies = page.locator('.radio-record-echo').evaluate_all('''nodes => nodes
      .filter(el => getComputedStyle(el).display !== 'none' && +el.getAttribute('opacity') > .08)
      .map(el => { const r = el.getBoundingClientRect();
        return {x: r.x + r.width * .6, y: r.y + r.height / 2, width: r.width}; })
      .filter(p => p.width > 25 && p.x > 0 && p.x < innerWidth && p.y > 0 && p.y < innerHeight)''')
    screenshot = Image.open(BytesIO(page.screenshot(
        path=f'/tmp/tuneconsole-radio-297-pixels-{color}-{page.viewport_size["width"]}.png'))).convert('RGB')

    def painted(x, y):
        red, green, blue = screenshot.getpixel((round(x), round(y)))
        if color == 'pink':
            return red > green + 10 and blue > green + 10
        return green > red + 10 and green > blue + 10

    assert painted(box['x'] + box['width'] * .7, box['y'] + box['height'] / 2), 'main label is not painted'
    assert copies and any(painted(p['x'], p['y']) for p in copies), {
        'message': 'recursive labels are not painted', 'copies': copies,
        'pixels': [screenshot.getpixel((round(p['x']), round(p['y']))) for p in copies]}


@pytest.mark.parametrize('width', [1440])
@pytest.mark.parametrize('failure', ['error', 'timeout'])
def test_record_uses_loaded_original_when_google_enlargement_fails(page, radio_ui, width, failure):
    page.set_viewport_size({'width': width, 'height': 1000})
    page.emulate_media(reduced_motion='reduce')
    page.evaluate('''sessionStorage.setItem('radio-artwork', JSON.stringify({
      playlistId: 'PLRADIO', phase: 0, seed: 284,
    }))''')
    tracks = radio_ui.tracks
    for index, track in enumerate(tracks[:3]):
        track['thumbnail'] = f'https://lh3.googleusercontent.com/radio-{index}=w120-h120-l90-rj'
    requests, held = [], []

    def artwork(route):
        url = route.request.url
        requests.append(url)
        if '/radio-2=' in url:
            route.fulfill(status=404)
        elif '/radio-0=' in url and '=w544-h544' in url:
            if failure == 'timeout':
                held.append(route)
            else:
                route.fulfill(status=403)
        else:
            fill = '#e83296' if '/radio-0=' in url else '#5fda23'
            route.fulfill(content_type='image/svg+xml', body=f'''<svg xmlns="http://www.w3.org/2000/svg"
              width="120" height="120"><path fill="{fill}" d="M0 0h120v120H0z"/></svg>''')

    page.route('https://lh3.googleusercontent.com/**', artwork)
    if failure == 'timeout':
        page.clock.install()
    templates = web_templates()
    template = templates.get_template('_partials/radio_playlist.html')
    radio_ui.state = {**radio_ui.ready, 'position': 0, 'history_count': 1}
    radio_ui.playlist = template.render(session=radio_ui.state, tracks=tracks)
    if failure == 'timeout':
        with page.expect_request('https://lh3.googleusercontent.com/radio-0=w544-h544-l90-rj'):
            page.clock.run_for(2100)
        page.wait_for_function('''() => Alpine.$data(document.querySelector('.radio-home'))
          .state.active && document.querySelector('.radio-record-cover')''')
        assert held
        page.clock.fast_forward(8100)
        page.clock.resume()
    cover = page.locator('.radio-record-cover')
    _assert_loaded_record_pixels(page, tracks[0]['thumbnail'], 'pink')
    row = page.locator('.radio-history .pl-thumb').first
    assert row.evaluate('el => el.complete && el.naturalWidth === 120')
    page.screenshot(path=f'/tmp/tuneconsole-radio-297-fallback-{failure}-{width}.png')
    # Even a late successful enlargement cannot undo the chosen original.
    for route in held:
        route.fulfill(content_type='image/svg+xml', body='''<svg xmlns="http://www.w3.org/2000/svg"
          width="544" height="544"><path fill="white" d="M0 0h544v544H0z"/></svg>''')
    initial_enlargements = sum('/radio-0=' in url and '=w544-h544' in url for url in requests)
    # A real playlist replacement and native track change exercise the SVG
    # lifecycle; ordinary polls retain the loaded fallback without retry loops.
    radio_ui.state = {**radio_ui.state, 'metadata_revision': 1}
    with page.expect_response('**/radio/playlist', timeout=8000):
        pass
    _assert_loaded_record_pixels(page, tracks[0]['thumbnail'], 'pink')
    assert sum('/radio-0=' in url and '=w544-h544' in url for url in requests) == initial_enlargements == 1
    before = page.locator('.radio-orbit-lines ellipse').evaluate_all('nodes => nodes.map(el => el.getAttribute("transform"))')
    page.evaluate('''video => window.dispatchEvent(new CustomEvent('bridge-status', {detail: {
      connected: true, now_playing: {video_id: video, playlist_ytm_id: 'PLRADIO'}
    }}))''', tracks[1]['video_id'])
    green = tracks[1]['thumbnail'].replace('w120-h120', 'w544-h544')
    _assert_loaded_record_pixels(page, green, 'green')
    page.wait_for_function('''before => [...document.querySelectorAll('.radio-orbit-lines ellipse')]
      .every((el, i) => el.getAttribute('transform') !== before[i])''', arg=before)
    page.screenshot(path=f'/tmp/tuneconsole-radio-297-track-change-{failure}-{width}.png')
    with page.expect_response(lambda response: response.url == tracks[2]['thumbnail']):
        page.evaluate('''video => window.dispatchEvent(new CustomEvent('bridge-status', {detail: {
          connected: true, now_playing: {video_id: video, playlist_ytm_id: 'PLRADIO'}
        }}))''', tracks[2]['video_id'])
    expect(cover).to_be_hidden()
    expect(page.locator('.radio-record-ground')).to_be_visible()
    expect(cover).not_to_have_attribute('data-loaded', green)
    # Missing artwork on one song must not poison another song in the session.
    page.evaluate('''video => window.dispatchEvent(new CustomEvent('bridge-status', {detail: {
      connected: true, now_playing: {video_id: video, playlist_ytm_id: 'PLRADIO'}
    }}))''', tracks[1]['video_id'])
    _assert_loaded_record_pixels(page, green, 'green')


def test_two_minute_landscapes_render_different_folds(page, radio_ui, tmp_path):
    templates = web_templates()
    radio_ui.playlist = templates.get_template('_partials/radio_playlist.html').render(
        session=radio_ui.ready, tracks=radio_ui.tracks)
    radio_ui.state = radio_ui.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    expect(page.locator('.radio-record-cover')).to_have_attribute('data-loaded', radio_ui.tracks[0]['thumbnail'])
    shapes = []
    for seed in (284, 2654435761, 1013904226, 3668339987):
        page.evaluate('''seed => {
          const model = Alpine.$data(document.querySelector('.radio-home'));
          model.pauseArtworkMotion();
          sessionStorage.setItem('radio-artwork', JSON.stringify({
            playlistId: 'PLRADIO', phase: Math.PI / 3, seed,
          }));
          model.artworkPlaylistId = null;
          model.syncArtworkSession();
        }''', seed)
        page.evaluate('''() => new Promise(resolve =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())))''')
        paths = page.locator('.radio-wave-landscape path').evaluate_all('''paths => paths.map(path => {
          const box = path.getBBox();
          return {d: getComputedStyle(path).d, width: box.width, height: box.height};
        })''')
        available_width = page.locator('.radio-engraving').bounding_box()['width']
        assert all(path['width'] >= available_width and path['height'] > 20 for path in paths)
        shapes.append([path['d'] for path in paths])
        page.locator('.radio-session').screenshot(path=tmp_path / f'radio-two-minutes-{seed}.png')
    assert len({tuple(shape) for shape in shapes}) == 4


@pytest.mark.parametrize('width,height', [(1440, 1000), (1792, 1398)])
def test_radio_starts_with_ordered_contours(page, radio_ui, width, height):
    page.set_viewport_size({'width': width, 'height': height})
    page.emulate_media(reduced_motion='reduce')
    page.evaluate('''sessionStorage.setItem('radio-artwork', JSON.stringify({
      playlistId: 'PLRADIO', phase: 0, seed: 284,
    }))''')
    templates = web_templates()
    radio_ui.playlist = templates.get_template('_partials/radio_playlist.html').render(
        session=radio_ui.ready, tracks=radio_ui.tracks)
    radio_ui.state = radio_ui.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    rendered_snapshot = '''() => ({
      phase: Alpine.$data(document.querySelector('.radio-home')).artworkPhase,
      copies: [...document.querySelectorAll('.radio-record-echo')].map(el => {
        const box = el.getBoundingClientRect();
        return {x: box.x, y: box.y, opacity: +el.getAttribute('opacity'),
          visible: getComputedStyle(el).display !== 'none', depth: +el.dataset.depth};
      }),
      rings: [...document.querySelectorAll('.radio-orbit-lines ellipse')]
        .map(el => el.getAttribute('transform')),
    })'''
    # Use real time first. A changed model clock alone cannot prove that the
    # Alpine SVG bindings keep moving in the actual On Air card.
    real_start = page.evaluate(rendered_snapshot)
    page.wait_for_function('''before => {
      const copies = [...document.querySelectorAll('.radio-record-echo')];
      return copies.some((el, i) => {
        const box = el.getBoundingClientRect(), old = before.copies[i];
        return old.visible && getComputedStyle(el).display !== 'none' &&
          Math.hypot(box.x-old.x, box.y-old.y)>2;
      }) && [...document.querySelectorAll('.radio-orbit-lines ellipse')]
        .every((el, i) => el.getAttribute('transform') !== before.rings[i]);
    }''', arg=real_start, timeout=8000)
    # Exercise an ordinary status/playlist replacement and a playing-track
    # change; neither may restart the session's motion or freeze its bindings.
    radio_ui.state = {**radio_ui.ready, 'position': 1, 'history_count': 2}
    radio_ui.playlist = templates.get_template('_partials/radio_playlist.html').render(
        session=radio_ui.state, tracks=radio_ui.tracks)
    with page.expect_response('**/radio/playlist', timeout=8000):
        pass
    page.evaluate('''video => window.dispatchEvent(new CustomEvent('bridge-status', {detail: {
      connected: true, now_playing: {video_id: video, playlist_ytm_id: 'PLRADIO'}
    }}))''', radio_ui.tracks[1]['video_id'])
    expect(page.locator('.radio-record-cover')).to_have_attribute('href', radio_ui.tracks[1]['thumbnail'])
    expect(page.locator('.radio-record-cover')).to_have_attribute('data-loaded', radio_ui.tracks[1]['thumbnail'])
    after_poll = page.evaluate(rendered_snapshot)
    assert after_poll['phase'] > real_start['phase']
    page.clock.install()
    samples = page.evaluate('''() => {
      const model = Alpine.$data(document.querySelector('.radio-home'));
      model.artworkPhase = 0;
      return new Promise(resolve => requestAnimationFrame(() => {
        const paths = [...document.querySelectorAll('.radio-wave-landscape path')];
        // Sample each contour at fixed columns, so crossings cannot hide
        // behind differences in individual path lengths.
        resolve([.1, .3, .5, .7, .9].map(fraction => paths.map(path => {
          let low = 0, high = path.getTotalLength();
          const x = model.drawingSpace.width * fraction;
          for (let i = 0; i < 24; i++) {
            const middle = (low + high) / 2;
            if (path.getPointAtLength(middle).x < x) low = middle;
            else high = middle;
          }
          return path.getPointAtLength((low + high) / 2).y;
        })));
      }));
    }''')
    for column in samples:
        assert all(lower - upper > 3 for upper, lower in zip(column, column[1:]))
    assert page.locator('.radio-record-echo:visible').count() > 0
    assert page.locator('.radio-record-echo').count() <= 24
    page.screenshot(path=f'/tmp/tuneconsole-radio-297-start-{width}.png')
    orbit_snapshot = '''() => {
      const model = Alpine.$data(document.querySelector('.radio-home'));
      return {copies: model.recordEchoes, width: model.drawingSpace.width,
        height: model.drawingSpace.historyHeight,
        rings: [...document.querySelectorAll('.radio-orbit-lines ellipse')]
          .map(el => el.getAttribute('transform')),
        transforms: [...document.querySelectorAll('.radio-record-echo')]
          .filter(el => getComputedStyle(el).display !== 'none')
          .map(el => el.getAttribute('transform'))};
    }'''
    def advance_listening_time(milliseconds):
        # Real wall-clock motion and poll continuity are checked above; the
        # separate motion test checks elapsed-time integration. Jump the long
        # listening timeline here, then render a frame to inspect its geometry.
        page.evaluate('''ms => {
          Alpine.$data(document.querySelector('.radio-home')).artworkPhase += ms * Math.PI * 2 / 720000;
        }''', milliseconds)
        page.clock.run_for(16)

    before = page.evaluate(orbit_snapshot)
    advance_listening_time(30_000)
    after = page.evaluate(orbit_snapshot)
    assert len(after['copies']) == len(before['copies'])
    assert after['transforms'] != before['transforms']
    assert after['rings'] != before['rings']
    assert any(abs(copy['x'] - initial['x']) + abs(copy['y'] - initial['y']) > 1
               for copy, initial in zip(after['copies'], before['copies']))
    for copy in (copy for copy in after['copies'] if copy['opacity'] > .005):
        assert 0 <= copy['x'] - copy['r'] < copy['x'] + copy['r'] <= after['width']
        assert 0 <= copy['y'] - copy['r'] < copy['y'] + copy['r'] <= after['height']
    page.screenshot(path=f'/tmp/tuneconsole-radio-297-orbit-{width}.png')
    # Inspect recursive geometry rendered at several natural listening times.
    # The fourth generation should emerge and withdraw while stable slots and
    # their parent links are retained.
    depths = []
    for step in range(12):
        advance_listening_time(5000)
        snapshot = page.evaluate(rendered_snapshot)
        depths.append(max((copy['depth'] for copy in snapshot['copies']
                           if copy['visible'] and copy['opacity'] > .01), default=0))
        if depths[-1] == 4 and 4 not in depths[:-1]:
            page.screenshot(path=f'/tmp/tuneconsole-radio-297-depth-{width}.png')
    if before['width'] > 1000:
        assert 4 in depths and min(depths) < 4, depths
