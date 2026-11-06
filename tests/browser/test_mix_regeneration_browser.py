"""Regeneration failures preserve the usable card and release it for a retry."""
import pytest
from playwright.sync_api import expect

from tests.home_fixture_support import browser_store as store, publish_menu, returning_listener


def test_regeneration_sends_the_held_recipe_after_each_animated_swap(live_app, store, page):
    import json
    from urllib.parse import parse_qs
    from yt_playlist.rec import choice_evidence

    # Publish four usable cards through their normal owner. This consumer test
    # exercises the modeled template's board refresh; preparation is separate.
    publish_menu(store, 1, modeled=True)
    returning_listener(store, 'mixes', now=1)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.goto(live_app + '/')
    card = page.locator('#gen-wheelhouse')
    expect(card).to_be_visible()
    card.locator('.gen-poster').click()
    body = card.locator('.gen-card-body')
    expect(card.get_by_role('button', name='Regenerate this mix', exact=True)).to_have_attribute(
        'hx-post', '/home/refresh-cards')
    closed = []
    for index in range(2):
        held = json.loads(body.get_attribute('data-recipe'))['opportunity_id']
        other = choice_evidence.present(store, 'mixes', [{'id': 'other'}], 1,
                                       context={}, window=f'other-tab-{index}')
        with page.expect_response('**/home/refresh-cards') as refreshed:
            card.get_by_role('button', name='Regenerate this mix', exact=True).click()
        response = refreshed.value
        assert response.status == 200
        assert parse_qs(response.request.post_data)['opportunity_id'] == [held]
        incoming = page.evaluate("""text => new DOMParser().parseFromString(text, 'text/html')
            .getElementById('gen-body-wheelhouse').dataset.recipe""", response.text())
        expect(body).to_have_attribute('data-recipe', incoming)
        # The recipe is installed before the incoming rows finish animating.
        # Complete each real swap before exercising the next held recipe.
        body.locator('.gen-focus .gen-row').evaluate_all('''rows => Promise.all(
            rows.flatMap(row => row.getAnimations()).map(animation => animation.finished))''')
        assert json.loads(incoming)['opportunity_id'] != held
        closed.append(held)
        responses = [row for row in store.rec_ledger.evidence().records('response') if row['kind'] == 'close']
        assert {row['opportunity_id'] for row in responses} == set(closed)
        assert all(row['opportunity_id'] != other for row in responses)
    card.screenshot(path='/tmp/tuneconsole-held-recipe-after-swaps.png')


@pytest.mark.parametrize('failure,modeled', [
    pytest.param('network', False, id='network'),
    pytest.param('http', False, id='http'),
    pytest.param('unprocessable', False, id='unprocessable'),
    pytest.param('missing', False, id='missing'),
    pytest.param('invalid', False, id='invalid'),
    pytest.param('empty', False, id='empty'),
    pytest.param('invalid', True, id='modeled-invalid'),
])
def test_mix_regeneration_recovers_from_failure(live_app, store, page, failure, modeled):
    publish_menu(store, 1, modeled=modeled)
    returning_listener(store, 'mixes', now=1)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # Keep the real Animation objects: a hover CSSTransition may still be
    # running after failure, independently of regeneration's looping motion.
    page.add_init_script("""(() => {
        window.regenerationAnimations = [];
        const animate = Element.prototype.animate;
        Element.prototype.animate = function (frames, options) {
            const animation = animate.call(this, frames, options);
            if (this.matches('#gen-wheelhouse .gen-focus .gen-row')
                    && options?.iterations === Infinity)
                window.regenerationAnimations.push(animation);
            return animation;
        };
    })();""")
    page.goto(f"{live_app}/")
    card = page.locator("#gen-wheelhouse")
    expect(card).to_be_visible()
    card.locator(".gen-poster").click()
    body = card.locator(".gen-card-body")
    button = card.get_by_role("button", name="Regenerate this mix", exact=True)
    # Exercise failure/retry on both actual template endpoint variants. The
    # modeled success path also has the repeated held-recipe integration above.
    endpoint = button.get_attribute('hx-post')
    assert endpoint == ('/home/refresh-cards' if modeled else '/home/refresh-card/wheelhouse')
    before = body.locator(".gen-row").evaluate_all("rows => rows.map(row => row.outerHTML)")
    replacement = body.evaluate("""body => {
        const next = body.cloneNode(true);
        next.dataset.name = 'Retried mix';
        next.querySelector('.gen-title').textContent = 'Replacement song';
        return next.outerHTML;
    }""")
    requests = []

    def respond(route):
        requests.append(route.request.url)
        if len(requests) > 1:
            route.fulfill(status=200, content_type="text/html", body=replacement)
        elif failure == "network":
            route.abort()
        elif failure == "http":
            # Even an error response containing the expected fragment must be rejected.
            route.fulfill(status=500, content_type="text/html", body=replacement)
        elif failure == "unprocessable":
            route.fulfill(status=422, content_type="text/html", body="Try again")
        elif failure == "invalid":
            route.fulfill(status=200, content_type="text/html",
                          body='<div id="gen-body-wheelhouse">Invalid mix</div>')
        elif failure == "empty":
            route.fulfill(status=204)
        else:
            route.fulfill(status=200, content_type="text/html", body="Missing mix")

    page.route(f"**{endpoint}", respond)
    # A second activation during the same request must not issue a duplicate POST.
    button.evaluate("button => { button.click(); regenerateMix(button); }")
    expect(card.get_by_role("alert")).to_contain_text("Try again")
    expect(button).to_be_enabled()
    expect(button).not_to_have_attribute("aria-busy", "true")
    expect(button).not_to_have_attribute("data-regenerating", "1")
    expect(card).not_to_have_class("is-regenerating")
    assert len(requests) == 1
    assert body.locator(".gen-row").evaluate_all("rows => rows.map(row => row.outerHTML)") == before
    assert page.evaluate("regenerationAnimations.map(animation => animation.playState)") == [
        'idle'] * len(before)
    # CSS hover transitions may finish or be superseded by another transition.
    body.locator('.gen-focus .gen-row').evaluate_all('''rows => Promise.allSettled(
        rows.flatMap(row => row.getAnimations()).map(animation => animation.finished))''')
    for title in body.locator('.gen-title').all():
        title.scroll_into_view_if_needed()
        expect(title).to_be_in_viewport()

    # The same button can retry successfully without a page reload.
    button.click()
    expect(body).to_have_attribute("data-name", "Retried mix")
    expect(body.locator(".gen-title").first).to_have_text("Replacement song")
    expect(card.get_by_role("alert")).to_have_count(0)
    expect(card).not_to_have_class("is-regenerating")
    expect(card.locator(".gen-refresh")).not_to_have_attribute("data-regenerating", "1")
    assert len(requests) == 2
    assert page.evaluate("regenerationAnimations.map(animation => animation.playState)") == [
        'idle'] * (2 * len(before))
    # The replacement's finite entrance is intentional and finishes naturally.
    body.locator('.gen-focus .gen-row').evaluate_all('''rows => Promise.allSettled(
        rows.flatMap(row => row.getAnimations()).map(animation => animation.finished))''')
    for title in body.locator('.gen-title').all():
        title.scroll_into_view_if_needed()
        expect(title).to_be_in_viewport()
    assert not errors
