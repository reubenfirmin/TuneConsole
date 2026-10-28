"""Regeneration failures preserve the usable card and release it for a retry."""
import pytest
from playwright.sync_api import expect

from tests.test_home_other_genres import seed_genres
from tests.test_mode_cards_route import prepare_home


def test_regeneration_sends_the_held_recipe_after_each_animated_swap(live_app, store, page):
    import json
    from urllib.parse import parse_qs
    from yt_playlist.rec import choice_evidence

    seed_genres(store)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    prepare_home(store, 1)
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.goto(live_app + '/')
    card = page.locator('#gen-wheelhouse')
    expect(card).to_be_visible()
    card.locator('.gen-poster').click()
    body = card.locator('.gen-card-body')
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
        assert json.loads(incoming)['opportunity_id'] != held
        closed.append(held)
        responses = [row for row in store.rec_ledger.evidence().records('response') if row['kind'] == 'close']
        assert {row['opportunity_id'] for row in responses} == set(closed)
        assert all(row['opportunity_id'] != other for row in responses)


@pytest.mark.parametrize("endpoint", ["/home/refresh-cards", "/home/refresh-card/wheelhouse"])
@pytest.mark.parametrize("failure", ["network", "http", "unprocessable", "missing", "invalid", "empty"])
def test_mix_regeneration_recovers_from_failure(live_app, store, page, endpoint, failure):
    seed_genres(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    prepare_home(store, 1)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{live_app}/")
    card = page.locator("#gen-wheelhouse")
    expect(card).to_be_visible()
    card.locator(".gen-poster").click()
    body = card.locator(".gen-card-body")
    button = card.get_by_role("button", name="Regenerate this mix", exact=True)
    # Both template variants share the same behavior with different POST endpoints.
    button.evaluate("""(button, endpoint) => {
        button.setAttribute('hx-post', endpoint);
        htmx.process(button);
    }""", endpoint)
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
    assert body.locator(".gen-row").evaluate_all(
        "rows => rows.flatMap(row => row.getAnimations()).length") == 0

    # The same button can retry successfully without a page reload.
    button.click()
    expect(body).to_have_attribute("data-name", "Retried mix")
    expect(body.locator(".gen-title").first).to_have_text("Replacement song")
    expect(card.get_by_role("alert")).to_have_count(0)
    expect(card).not_to_have_class("is-regenerating")
    expect(card.locator(".gen-refresh")).not_to_have_attribute("data-regenerating", "1")
    assert len(requests) == 2
    assert not errors
