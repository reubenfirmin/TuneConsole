"""The composed starting landscape stays ordered at desktop and phone sizes."""
import pytest
from tests.template_support import web_templates
from playwright.sync_api import expect

from tests.browser.radio_ui_support import radio_ui  # noqa: F401


def test_two_minute_landscapes_render_different_folds(page, radio_ui, tmp_path):
    templates = web_templates()
    radio_ui.playlist = templates.get_template('_partials/radio_playlist.html').render(
        session=radio_ui.ready, tracks=radio_ui.tracks)
    radio_ui.state = radio_ui.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
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
        assert all(path['width'] > 1000 and path['height'] > 20 for path in paths)
        shapes.append([path['d'] for path in paths])
        page.locator('.radio-session').screenshot(path=tmp_path / f'radio-two-minutes-{seed}.png')
    assert len({tuple(shape) for shape in shapes}) == 4


@pytest.mark.parametrize('width,height', [(1440, 1000), (1792, 1398), (390, 1000)])
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
        if depths[-1] == 4:
            page.screenshot(path=f'/tmp/tuneconsole-radio-297-depth-{width}.png')
    if width > 1000:
        assert 4 in depths and min(depths) < 4, depths
