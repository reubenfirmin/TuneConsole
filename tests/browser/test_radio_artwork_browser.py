"""The composed starting landscape stays ordered at desktop and phone sizes."""
import pytest
from fastapi.templating import Jinja2Templates
from playwright.sync_api import expect

from tests.browser.radio_ui_support import radio_ui  # noqa: F401


@pytest.mark.parametrize('width,height', [(1440, 1000), (1792, 1398), (390, 1000)])
def test_radio_starts_with_ordered_contours(page, radio_ui, width, height):
    page.set_viewport_size({'width': width, 'height': height})
    templates = Jinja2Templates(directory='src/yt_playlist/web/templates')
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
    page.screenshot(path=f'/tmp/tuneconsole-radio-284-start-{width}.png')
