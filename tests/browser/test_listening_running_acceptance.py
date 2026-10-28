"""Opt-in acceptance of the actual served edition, with no writes to the user's library."""
import json
from pathlib import Path

from playwright.sync_api import expect
import pytest


@pytest.mark.parametrize('width', [1440, 390])
def test_running_charts_has_useful_variety(page, request, width):
    url = request.config.getoption('--listening-acceptance-url')
    if not url:
        pytest.skip('Pass --listening-acceptance-url to inspect the running application')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/charts/listening-impressions', lambda route: route.fulfill(json={'ok': True}))
    page.set_viewport_size({'width': width, 'height': 1100})
    page.goto(url.rstrip('/')+'/charts?view=home')
    journal = page.locator('[data-listening-edition]')
    raw = journal.get_attribute('x-data')
    edition = json.loads(raw[len('listeningEditorial('):-1])
    stories = edition['stories']
    assert len(stories) == 4, [(s['family'], s['title']) for s in stories]
    families = {s['family'] for s in stories}
    assert families & {'attachment', 'familiar', 'return', 'deeper', 'rising'}
    assert {'discovery', 'rating_collection'} <= families
    assert 'decision' not in families
    expect(journal.locator('.le-story')).to_have_count(len(stories))
    for story in stories:
        panel = journal.locator(f'[data-story-id="{story["id"]}"]')
        panel.scroll_into_view_if_needed()
        expect(panel.get_by_role('heading')).to_have_text(story['title'])
        expect(panel.locator('.le-composition > svg')).to_be_visible()
    assert journal.locator('.le-composition > svg').evaluate_all(
        "els => els.every(el => !/NaN|Infinity/.test(el.innerHTML))")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.evaluate('document.fonts.ready')
    page.screenshot(path=f'/tmp/tuneconsole-live-charts-{width}.png', full_page=True)
    for visual in ('discovery', 'rating_genres'):
        journal.locator('.le-'+visual).screenshot(path=f'/tmp/tuneconsole-live-{visual}-{width}.png')
    Path(f'/tmp/tuneconsole-live-charts-{width}.json').write_text(json.dumps(edition, indent=2))
    page.locator('.chart-views').get_by_role('button',name='Listening analytics',exact=True).click()
    analytics=page.locator('[data-listening-analytics]')
    expect(analytics.locator('.lp-summary')).to_be_visible()
    rates=analytics.get_by_role('region',name='Discovery rates')
    expect(rates).to_be_visible()
    expect(rates.get_by_text('Tracks new to this history',exact=True)).to_be_visible()
    assert analytics.evaluate("el => Alpine.$data(el).result.discovery.start")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    rates.screenshot(path=f'/tmp/tuneconsole-live-analytics-discovery-{width}.png')
    assert not errors, errors
