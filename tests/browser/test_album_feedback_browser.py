"""The saved-feedback screen exposes one reversible judgment per verified release."""
from playwright.sync_api import expect

from tests.repos.test_album_aliases import OLD, ALIAS, release
from yt_playlist.rec import album_choices


def test_alias_feedback_renders_once_and_clears_without_resurrecting_an_old_state(live_app, store, page, tmp_path):
    store.settings.set_setting('onboard_dismissed', '1')
    store.settings.set_setting('last_sync_at', '1')
    album_choices.set_feedback(store, OLD, 'known', 0)
    album_choices.set_feedback(store, ALIAS, 'reject', 1)
    for browse in (OLD, ALIAS):
        store.collection.observe_album_representation(browse, release(), now=1)
    page.goto(live_app + '/taste#feedback')
    feedback = page.get_by_role('region', name='Album feedback', exact=True)
    expect(feedback.locator('form')).to_have_count(1)
    expect(feedback.get_by_text('Not for me', exact=True)).to_be_visible()
    expect(feedback.get_by_role('link')).to_have_attribute('href', '/album?browse=' + OLD)
    page.screenshot(path=str(tmp_path / 'verified-album-feedback.png'), full_page=True)
    with page.expect_response(lambda response: response.url.endswith('/album/feedback')) as response:
        feedback.get_by_role('button', name='Clear album feedback', exact=True).click()
    assert response.value.status == 204
    expect(feedback.locator('form')).to_have_count(0)
    assert album_choices.feedback_state(store, [OLD, ALIAS]) == {}
    page.reload()
    expect(page.get_by_role('heading', name='Your saved feedback', exact=True)).to_be_visible()
    expect(feedback).to_have_count(0)
