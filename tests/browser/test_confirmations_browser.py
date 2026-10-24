"""Shared htmx confirmations own one request and a complete modal lifecycle."""
import pytest
from playwright.sync_api import expect


@pytest.fixture
def confirmation_page(page, live_app):
    page.set_default_timeout(10000)
    requests = []
    page.route('**/confirmation-check/*', lambda route: (
        requests.append(route.request.url), route.fulfill(status=204)))
    page.goto(live_app + '/charts')
    page.evaluate("""() => {
      const fixture = document.createElement('section'); fixture.id = 'confirmation-fixture';
      fixture.innerHTML = '<button id="first-confirm" hx-get="/confirmation-check/first" '
        + 'hx-swap="none" hx-confirm="First question">First action</button>'
        + '<button id="second-confirm" hx-get="/confirmation-check/second" '
        + 'hx-swap="none" hx-confirm="Second question">Second action</button>';
      document.querySelector('main').prepend(fixture); htmx.process(fixture);
      window.backgroundKeys = [];
      window.addEventListener('keydown', event => backgroundKeys.push(event.key));
    }""")
    return requests


def test_confirmation_contains_focus_shortcuts_and_restores_invoker(page, confirmation_page):
    first = page.get_by_role('button', name='First action', exact=True)
    first.click()
    dialog = page.get_by_role('dialog', name='Confirm', exact=True)
    expect(dialog).to_be_visible()
    expect(dialog).to_have_attribute('aria-describedby', 'shared-confirmation-question')
    confirm = dialog.get_by_role('button', name='Confirm', exact=True)
    cancel = dialog.get_by_role('button', name='Cancel', exact=True)
    expect(confirm).to_be_focused()
    for _ in range(3):
        page.keyboard.press('Tab')
        expect(cancel).to_be_focused()
        page.keyboard.press('Tab')
        expect(confirm).to_be_focused()
    page.keyboard.press('Shift+Tab')
    expect(cancel).to_be_focused()
    page.keyboard.press('Shift+Tab')
    expect(confirm).to_be_focused()
    page.evaluate('document.getElementById("second-confirm").focus()')
    expect(confirm).to_be_focused()  # native showModal makes background controls inert
    page.keyboard.press('MediaTrackNext')
    page.keyboard.press('Escape')
    expect(dialog).to_have_count(0)
    expect(first).to_be_focused()
    assert page.evaluate('backgroundKeys') == []
    assert confirmation_page == []
    first.click()
    dialog.get_by_role('button', name='Cancel', exact=True).click()
    expect(first).to_be_focused()
    expect(dialog).to_have_count(0)


def test_repeated_confirmation_keeps_one_owner_and_only_confirms_its_request(page, confirmation_page):
    page.get_by_role('button', name='First action', exact=True).click()
    page.evaluate("htmx.trigger('#second-confirm', 'click')")
    dialog = page.get_by_role('dialog', name='Confirm', exact=True)
    expect(dialog).to_have_count(1)
    expect(dialog).to_contain_text('First question')
    dialog.get_by_role('button', name='Confirm', exact=True).click()
    page.wait_for_function('document.querySelectorAll("dialog.shared-confirmation").length === 0')
    assert len(confirmation_page) == 1 and confirmation_page[0].endswith('/first')
    page.get_by_role('button', name='Second action', exact=True).click()
    expect(dialog).to_contain_text('Second question')
    dialog.get_by_role('button', name='Confirm', exact=True).click()
    expect(dialog).to_have_count(0)
    assert len(confirmation_page) == 2 and confirmation_page[1].endswith('/second')


@pytest.mark.parametrize('cleanup', ['htmx', 'native'])
def test_disposing_confirmation_invoker_releases_modal_and_pending_request(page, confirmation_page, cleanup):
    page.get_by_role('button', name='First action', exact=True).click()
    page.evaluate("""cleanup => {
      const fixture = document.getElementById('confirmation-fixture');
      if (cleanup === 'htmx') htmx.remove(fixture); else fixture.remove();
    }""", cleanup)
    expect(page.get_by_role('dialog', name='Confirm', exact=True)).to_have_count(0)
    assert confirmation_page == []
    page.keyboard.press('Escape')
    assert page.evaluate('backgroundKeys') == ['Escape']
    # A later invoker can open a fresh confirmation after the disposed request is released.
    page.evaluate("""() => {
      const button = document.createElement('button'); button.textContent = 'Later action';
      button.setAttribute('hx-confirm', 'Later question');
      button.setAttribute('hx-get', '/confirmation-check/later');
      button.setAttribute('hx-swap', 'none'); document.querySelector('main').prepend(button); htmx.process(button);
    }""")
    page.get_by_role('button', name='Later action').click()
    dialog = page.get_by_role('dialog', name='Confirm', exact=True)
    expect(dialog).to_contain_text('Later question')
    dialog.get_by_role('button', name='Confirm', exact=True).click()
    assert len(confirmation_page) == 1 and confirmation_page[0].endswith('/later')
