"""Enter the festival through its ordinary welcome action."""
from playwright.sync_api import expect


def dismiss_welcome(page):
    welcome = page.get_by_role('dialog', name='A better way to listen to music.', exact=True)
    expect(welcome).to_be_visible()
    with page.expect_response(lambda response: response.url.endswith('/onboard/welcome/dismiss')
                              and response.request.method == 'POST') as acknowledgment:
        welcome.get_by_role('button', name='Explore my festival', exact=True).click()
    assert acknowledgment.value.ok
    expect(welcome).to_be_hidden()
