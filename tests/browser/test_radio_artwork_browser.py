"""The composed starting landscape stays ordered at desktop and phone sizes."""
import pytest
from tests.template_support import web_templates
from playwright.sync_api import expect

from tests.browser.radio_ui_support import radio_ui  # noqa: F401


@pytest.mark.parametrize('width,height', [(1440, 1000), (1792, 1398), (390, 1000)])
def test_radio_starts_with_ordered_contours(page, radio_ui, width, height):
    page.set_viewport_size({'width': width, 'height': height})
    templates = web_templates()
    radio_ui.playlist = templates.get_template('_partials/radio_playlist.html').render(
        session=radio_ui.ready, tracks=radio_ui.tracks)
    radio_ui.state = radio_ui.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
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
    expect(page.locator('.radio-record-echo').first).to_be_visible()
    assert page.locator('.radio-record-echo').count() <= 24
    page.screenshot(path=f'/tmp/tuneconsole-radio-297-start-{width}.png')
    orbit_snapshot = '''() => {
      const model = Alpine.$data(document.querySelector('.radio-home'));
      return {copies: model.recordEchoes, width: model.drawingSpace.width,
        height: model.drawingSpace.historyHeight,
        transforms: [...document.querySelectorAll('.radio-record-echo')]
          .filter(el => getComputedStyle(el).display !== 'none')
          .map(el => el.getAttribute('transform'))};
    }'''
    before = page.evaluate(orbit_snapshot)
    page.clock.run_for(30_000)
    after = page.evaluate(orbit_snapshot)
    assert len(after['copies']) == len(before['copies'])
    assert after['transforms'] != before['transforms']
    assert any(abs(copy['x'] - initial['x']) + abs(copy['y'] - initial['y']) > 1
               for copy, initial in zip(after['copies'], before['copies']))
    for copy in after['copies']:
        assert 0 <= copy['x'] - copy['r'] < copy['x'] + copy['r'] <= after['width']
        assert 0 <= copy['y'] - copy['r'] < copy['y'] + copy['r'] <= after['height']
    page.screenshot(path=f'/tmp/tuneconsole-radio-297-orbit-{width}.png')
