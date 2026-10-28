"""Track artwork falls back once and hides only images that cannot load."""
from pathlib import Path

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('outcome', ['primary', 'fallback', 'both-fail', 'same-fallback'])
def test_track_artwork_loading(live_app, store, page, tmp_path, width, outcome):
    fallback = 'https://art.googleusercontent.com/cover=w120-h120'
    primary = fallback.replace('w120-h120', 'w800-h800')
    if outcome == 'same-fallback':
        primary = fallback = 'https://art.googleusercontent.com/missing-cover.jpg'
    store.tracks.upsert_track('artwork-control', 'Artwork control', 'Fixture artist', 'Album', 180,
                             thumbnail=fallback)
    requests, errors = [], []
    image = (Path(__file__).resolve().parents[2] /
             'src/yt_playlist/web/static/nag/identities.png').read_bytes()

    def artwork(route):
        requests.append(route.request.url)
        success = (outcome == 'primary' and route.request.url == primary or
                   outcome == 'fallback' and route.request.url == fallback)
        if success:
            route.fulfill(content_type='image/png', body=image)
        else:
            route.abort()

    page.route('https://art.googleusercontent.com/**', artwork)
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(live_app + '/track/artwork-control')
    art = page.locator('.track-detail-art')
    if outcome in ('primary', 'fallback'):
        expect(art).to_be_visible()
        page.wait_for_function("document.querySelector('.track-detail-art').naturalWidth > 0")
        expect(art).to_have_attribute('src', primary if outcome == 'primary' else fallback)
    else:
        expect(art).to_have_css('visibility', 'hidden')
        assert art.evaluate('el => el.complete && el.naturalWidth === 0')
    # An identical failed fallback must terminate without requesting the image again.
    expected = [primary, fallback] if outcome in ('fallback', 'both-fail') else [primary]
    assert requests == expected
    assert not errors, errors
    expect(page.get_by_role('heading', name='Artwork control', exact=True)).to_be_visible()
    expect(page.get_by_role('link', name='Play on YouTube Music', exact=True)).to_be_visible()
    expect(page.get_by_role('heading', name='Listening history', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=tmp_path / f'track-artwork-{outcome}-{width}.png')
