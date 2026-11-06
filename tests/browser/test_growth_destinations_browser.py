"""Real projections drive tab visibility and newly available destination content."""
import pytest
from pathlib import Path
from playwright.sync_api import expect

from tests.onboarding_support import publish_personal_mix
from tests.test_growth_destinations import publish_full_journey
from tests.test_growth_journey import early_journey


@pytest.mark.parametrize('width', [1536])
def test_destinations_appear_with_real_content_and_keep_the_player(live_app, store, page, width):
    early_journey(store)
    publish_personal_mix(store, now=1)
    prepared = store.rec_proposals.get_proposals('home_tuned')
    prepared['fingerprint']['breadth'] = {'families': {'house': 1}, 'breadth': 0}
    store.rec_proposals.put_proposals('home_tuned', prepared, 1)
    store.settings.set_setting('last_sync_at', '1')
    # A remembered adjustment cannot reveal the Vibes controls on its own.
    store.settings.set_setting('home_mix_vibe', 'relax')
    bridge = {'connected': True, 'now_playing': {
        'video_id': 'playing', 'title': 'Still playing', 'artist': 'Artist', 'paused': False}}
    page.route('**/bridge/status', lambda route: route.fulfill(json=bridge))
    page.route('https://i.ytimg.com/**', lambda route: route.fulfill(status=404))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/#tv')
    expect(page.get_by_role('tab', name='Your unlocks')).to_have_attribute('aria-selected', 'true')
    recent_route = page.locator('.festival-routes [data-destination="recent"]')
    expect(recent_route).to_have_attribute('data-parent', 'vibes')
    original_route = recent_route.locator('path').last.get_attribute('d')
    for label in ('TV', 'Revisit', 'Into recently'):
        expect(page.get_by_role('tab', name=label, exact=True)).to_be_hidden()
    mixes = page.get_by_role('tab', name='Mix tapes', exact=True)
    mixes.click()
    mixes.press('Home')
    expect(mixes).to_be_focused()
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    expect(page.locator('#fp-dialog')).to_be_visible()
    expect(page.locator('#fp-vibe-tab')).to_be_hidden()
    expect(page.get_by_role('tab', name='Genre focus', exact=True)).to_have_attribute('aria-selected', 'true')
    page.get_by_role('button', name='Close mix controls').click()
    page.evaluate('window.growthDocument = document; window.growthPlayer = document.querySelector(".global-now-playing")')
    # The server is serving the same Store now. Fixture catalog inserts must
    # serialize with its seed preparation and feedback writes.
    with store._lock:
        publish_full_journey(store, now=1)
    page.evaluate('() => Alpine.$data(document.querySelector(".home-growth")).refresh()')
    for label in ('TV', 'Discover', 'Revisit', 'Into recently'):
        expect(page.get_by_role('tab', name=label, exact=True)).to_be_visible()
    notice = page.locator('.growth-unlock-notice')
    # Earned notices need their own space beside the real Mix and persistent TV
    # surfaces. Sample every text line, including both edges of the heading;
    # a box or center-only check misses the partially covered TV reproduction.
    for connected in (False, True):
        bridge['connected'] = connected
        page.evaluate('window.playbackStatus.refresh()')
        for label, panel in (('Mix tapes', 'directions'), ('TV', 'tv')):
            page.get_by_role('tab', name=label, exact=True).click()
            expect(page.locator(f'[data-dash="{panel}"]')).to_be_visible()
            page.wait_for_function("connected => Alpine.$data(document.querySelector('#home')).connected === connected", arg=connected)
            if connected:
                expect(page.locator('.onboard-card')).to_be_hidden()
            else:
                expect(page.locator('.onboard-card')).to_be_visible()
            expect(notice).to_be_visible()
            page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            page.locator('main').evaluate('''el => Promise.all(el.getAnimations({subtree:true})
                .filter(a => a.effect.getComputedTiming().iterations !== Infinity)
                .map(a => a.finished.catch(() => {})))''')
            if panel == 'directions':
                page.wait_for_function('''() => [...document.querySelectorAll('#home-mode-cards .gen-poster')]
                    .every(el => { for (let parent = el; parent; parent = parent.parentElement) {
                        if (getComputedStyle(parent).opacity !== '1') return false;
                    } return el.checkVisibility({checkOpacity:true}); })''')
            composition = notice.evaluate('''notice => {
                const n = notice.getBoundingClientRect();
                const walker = document.createTreeWalker(notice, NodeFilter.SHOW_TEXT);
                const obscured = [];
                while (walker.nextNode()) {
                    const node = walker.currentNode;
                    if (!node.textContent.trim()) continue;
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    for (const r of range.getClientRects()) {
                        if (!r.width || !r.height) continue;
                        for (const x of [r.left + 1, (r.left + r.right) / 2, r.right - 1]) {
                            const top = document.elementFromPoint(x, (r.top + r.bottom) / 2);
                            if (!top || !notice.contains(top)) obscured.push(node.textContent.trim());
                        }
                    }
                }
                const protectedContent = [...document.querySelectorAll(
                    '[data-dash="directions"] .gen-poster, [data-dash="directions"] .gen-theme, .tv-session')]
                    .filter(el => el.getClientRects().length)
                    .map(el => { const r = el.getBoundingClientRect();
                        return {element: el.className, overlap:
                            Math.max(0, Math.min(n.right,r.right)-Math.max(n.left,r.left)) *
                            Math.max(0, Math.min(n.bottom,r.bottom)-Math.max(n.top,r.top))}; });
                return {obscured, protectedContent, noticeTop: n.top, noticeBottom: n.bottom,
                    pageScroll: scrollY, viewportHeight: innerHeight};
            }''')
            page.screenshot(path=f'/tmp/tuneconsole-985-notice-{connected}-{panel}-{width}.png', full_page=True)
            assert composition['obscured'] == [], composition
            assert composition['protectedContent'], composition
            assert all(item['overlap'] == 0 for item in composition['protectedContent']), composition
    page.get_by_role('tab', name='Mix tapes', exact=True).click()
    # Preparation changes lighting and access; the illustrated route stays fixed.
    expect(recent_route).to_have_attribute('data-parent', 'vibes')
    new_route = recent_route.locator('path').last.get_attribute('d')
    assert new_route == original_route
    # Prepared/ready output has not been explored. Its real trail is visible in
    # the scenery, with no SVG route or shadow painted over it.
    expect(recent_route).to_be_hidden()
    expect(page.locator('.growth-route-shadow')).to_have_count(0)
    expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_have_attribute('aria-selected', 'true')
    page.get_by_role('tab', name='Discover', exact=True).click()
    expect(page.locator('#discovery-track-grid')).to_contain_text('New 0')
    expect(page.locator('#home-vibe-feedback')).to_be_hidden()
    with page.expect_response(lambda response: response.url.endswith('/home/revisit')) as requested_revisit:
        page.get_by_role('tab', name='Revisit', exact=True).click()
    revisit = requested_revisit.value
    assert revisit.status == 200, revisit.text()
    assert 'Old favorite' in revisit.text(), revisit.text()
    assert not errors, errors
    expect(page.locator('#rediscover')).to_contain_text('Old favorite')
    expect(page.locator('[data-dash="rediscover"] > div')).to_have_attribute('data-loaded', '1')
    page.get_by_role('tab', name='Into recently', exact=True).click()
    expect(page.locator('[data-dash="recent"]')).to_contain_text('Jazz brings improvisation')
    assert page.evaluate('growthDocument === document && growthPlayer === document.querySelector(".global-now-playing")')
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    page.get_by_role('tab', name='Your unlocks').click()
    expect(page.locator('#home-vibe-feedback')).to_be_hidden()
    expect(page.locator('.festival-destination')).to_have_count(14)
    # Signs are on clear ground below their own venue, outside its cutout.
    assert page.locator('.festival-destination').evaluate_all('''stops => stops.every(stop => {
        const sign = stop.querySelector('.landmark-sign');
        const position = sign.transform.baseVal.consolidate().matrix;
        return !stop.querySelector('.landmark-hit').isPointInFill(new DOMPoint(position.e, position.f));
    })''')
    for _ in range(page.evaluate('Alpine.$data(document.querySelector(".home-growth")).newNodes.length')):
        page.locator('.growth-unlock-notice').get_by_role('button', name='Mark as seen').click()
    page.get_by_role('button', name='Later', exact=True).click()
    assert page.locator('body').evaluate('el => el.scrollWidth <= innerWidth')
    page.locator('main').evaluate('el => Promise.all(el.getAnimations({subtree:true}).filter(a => a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished))')
    page.screenshot(path=f'/tmp/tuneconsole-564-destinations-{width}.png', full_page=True)
    for asset in ('festival-grounds-v4.png', 'account-growth.css', 'account-growth.js'):
        response = page.request.get(live_app + '/static/' + asset)
        assert response.status == 200
        assert response.body() == (Path(__file__).resolve().parents[2] /
            'src/yt_playlist/web/static' / asset).read_bytes()
    for label in ('Your music', 'Long play', 'Mix tapes', 'On air', 'TV', 'Vibes', 'Discover',
                  'Revisit', 'Into recently', 'Past listening', 'Music details',
                  'Album stories', 'Little victories'):
        stop = page.locator('.festival-destination').filter(has=page.locator('.landmark-label', has_text=label))
        stop.locator('.landmark-hit').hover()
        assert stop.evaluate('''stop => {
            const label = getComputedStyle(stop.querySelector('.landmark-label'));
            const hit = getComputedStyle(stop.querySelector('.landmark-hit'));
            return label.fill === 'rgb(89, 234, 255)' && label.filter.includes('drop-shadow')
                && hit.stroke === 'rgba(0, 0, 0, 0)';
        }''')
        if label in ('Long play', 'Mix tapes', 'Vibes', 'Into recently'):
            page.screenshot(path=f'/tmp/tuneconsole-612-hover-{label.replace(" ", "-")}-{width}.png')
        stop.focus()
        # Enter keyboard mode and return to this landmark. The label provides
        # the focus cue while the artwork stays free of polygon outlines.
        page.keyboard.press('Tab')
        page.keyboard.press('Shift+Tab')
        expect(stop).to_be_focused()
        focus = stop.evaluate('''stop => ({visible: stop.matches(':focus-visible'),
            fill: getComputedStyle(stop.querySelector('.landmark-label')).fill,
            glow: getComputedStyle(stop.querySelector('.landmark-label')).filter.includes('drop-shadow'),
            stroke: getComputedStyle(stop.querySelector('.landmark-hit')).stroke})''')
        assert focus == {'visible': True, 'glow': True, 'fill': 'rgb(89, 234, 255)',
                         'stroke': 'rgba(0, 0, 0, 0)'}, focus
        stop.press('Enter')
        expect(page.locator('.growth-detail')).to_be_visible()
        if label == 'Little victories':
            expect(page.locator('.growth-achievements li')).to_have_count(3)
        if label == 'Vibes':
            page.locator('.growth-detail').evaluate('el => Promise.all(el.getAnimations().map(a => a.finished))')
            page.screenshot(path=f'/tmp/tuneconsole-564-vibes-{width}.png', full_page=True)
        page.keyboard.press('Escape')
        expect(page.locator('.growth-detail')).not_to_be_visible()
    # A quick close followed by another destination leaves the old native close
    # event queued. It must not erase the newly opened destination's contents.
    page.evaluate('''() => {
        const journey = Alpine.$data(document.querySelector('.home-growth'));
        journey.openNode('albums');
        journey.destinationPopup.close();
        journey.openNode('milestones');
    }''')
    expect(page.get_by_role('dialog', name='Little victories', exact=True)).to_be_visible()
    expect(page.locator('.growth-achievements li')).to_have_count(3)
    page.keyboard.press('Escape')
    expect(page.locator('.growth-detail')).not_to_be_visible()
    assert not errors
