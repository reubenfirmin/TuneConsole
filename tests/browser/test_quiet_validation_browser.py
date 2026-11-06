"""Rendered controls retain state across genuine provider parsing failures."""
import socket

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.browser.quiet_action_support import action_evidence, assert_quiet_action
from tests.live_server import LiveServer
from tests.quiet_validation_support import DIAGNOSTIC, prepare_quiet_actions

pytestmark = pytest.mark.browser


@pytest.fixture
def quiet_actions_app(store, monkeypatch):
    data = prepare_quiet_actions(store, monkeypatch)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(data['app'], host='127.0.0.1',
            port=listener.getsockname()[1], log_level='warning'))
        with LiveServer(server, sockets=[listener]):
            yield {**data, 'base': f'http://127.0.0.1:{listener.getsockname()[1]}'}


def test_like_provider_failure_rolls_back_heart_without_toast_or_ui_replay(
        quiet_actions_app, page, action_evidence):
    case, provider = quiet_actions_app, quiet_actions_app['main']
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(f"{case['base']}/playlist/{case['source']}")
    row = page.locator('tr.srow[data-vid="owned-song"]')
    heart = row.locator('.like-btn')
    expect(heart).to_have_attribute('aria-pressed', 'false')
    provider.fault, provider.kind, provider.after = 'rate_song', 'json', True
    with page.expect_response('**/track/like') as response:
        heart.click()
    assert response.value.status == 503
    expect(heart).to_have_attribute('aria-pressed', 'false')
    expect(heart).to_be_enabled()
    expect(row).to_contain_text('Owned Song')
    assert provider.calls.count('rate_song') == 3
    assert provider.rated == [('owned-song', 'LIKE')] * 3
    assert case['store'].tracks.item_rating(case['main_id'], 'owned-song') is None
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-like-provider-failure.png')
    page.reload()
    expect(heart).to_have_attribute('aria-pressed', 'false')
    assert provider.calls.count('rate_song') == 3
    provider.fault = None
    heart.click()
    expect(heart).to_have_attribute('aria-pressed', 'true')
    assert provider.calls.count('rate_song') == 4
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-like-explicit-recovery.png')


@pytest.mark.parametrize('saved,boundary,kind,after', [
    (False, 'get_album', 'json', False),
    (True, 'rate_playlist', 'value', True),
])
def test_album_provider_failure_preserves_membership_and_releases_control(
        quiet_actions_app, page, kind, saved, boundary, after, action_evidence):
    case, provider = quiet_actions_app, quiet_actions_app['main']
    store = case['store']
    if saved:
        store.collection.add_saved_album({'browse': 'MPRE_new', 'title': 'New Album'}, account_id=case['main_id'])
        provider.saved.add('QUIET-AUDIO')
    before = store.collection.saved_album_owners('MPRE_new')
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(case['base'] + '/album?browse=MPRE_new')
    path = '/collection/' + ('unsave-album' if saved else 'save-album')
    button = page.locator(f'form[hx-post="{path}"] button')
    expect(button).to_be_enabled()
    provider.calls.clear()
    provider.fault, provider.kind, provider.after = boundary, kind, after
    with page.expect_response('**' + path) as response:
        button.click()
    assert response.value.status == 503
    expect(button).to_be_enabled()
    expect(page.get_by_role('heading', name='New Album', exact=True)).to_be_visible()
    assert store.collection.saved_album_owners('MPRE_new') == before
    assert provider.calls == ['get_album'] + (['rate_playlist'] if boundary == 'rate_playlist' else [])
    assert provider.album_ratings == ([('QUIET-AUDIO', 'INDIFFERENT' if saved else 'LIKE')]
                                      if boundary == 'rate_playlist' and after else [])
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-album-provider-failure.png')
    writes = provider.calls.count('rate_playlist')
    provider.fault = None
    page.reload()
    expect(button).to_be_enabled()
    assert store.collection.saved_album_owners('MPRE_new') == before
    assert provider.calls.count('rate_playlist') == writes
    button.click()  # Explicit recovery, including an idempotent unconfirmed remote rating.
    expect(page.locator(f'form[hx-post="/collection/{"save-album" if saved else "unsave-album"}"] button')).to_be_enabled()
    assert ('MPRE_new' in store.collection.saved_album_ids()) is not saved
    assert ('QUIET-AUDIO' in provider.saved) is not saved
    assert provider.calls.count('rate_playlist') == writes + 1
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-album-explicit-recovery.png')


def create_dialog(case, page):
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(f"{case['base']}/playlist/{case['source']}")
    page.get_by_role('button', name='More actions for Owned Song').click()
    page.get_by_role('menuitem', name='Create playlist from song', exact=True).click()
    dialog = page.get_by_role('dialog', name='Create playlist', exact=True)
    dialog.get_by_label('Playlist name').fill('Keep this playlist draft')
    return dialog


@pytest.mark.parametrize('kind,after', [('value', False), ('json', True)])
def test_create_provider_failure_keeps_draft_and_does_not_repeat_uncertain_creation(
        quiet_actions_app, page, kind, after, action_evidence):
    case, provider = quiet_actions_app, quiet_actions_app['main']
    before = case['store'].playlists.get_playlists()
    dialog = create_dialog(case, page)
    provider.fault, provider.kind, provider.after = 'create_playlist', kind, after
    with page.expect_response('**/songs/create-playlist') as response:
        dialog.get_by_role('button', name='Create playlist', exact=True).click()
    assert response.value.status == 502
    expect(dialog.get_by_text('This change was not confirmed. Review it before trying again.', exact=True)).to_be_visible()
    expect(dialog.get_by_label('Playlist name')).to_have_value('Keep this playlist draft')
    expect(dialog.get_by_role('button', name='Create playlist', exact=True)).to_be_enabled()
    assert provider.calls.count('create_playlist') == 1
    assert len(provider.created) == int(after) and not provider.added
    assert case['store'].playlists.get_playlists() == before
    assert_quiet_action(page, unconfirmed_song_action=True)
    page.screenshot(path=action_evidence + '-create-provider-failure.png')
    dialog.get_by_role('button', name='Cancel', exact=True).click()
    page.reload()
    assert provider.calls.count('create_playlist') == 1 and len(provider.created) == int(after)
    assert case['store'].playlists.get_playlists() == before
    assert_quiet_action(page)


def test_dialog_rejects_unrecognized_validation_text_after_actual_provider_failure(
        quiet_actions_app, page, action_evidence):
    case, provider = quiet_actions_app, quiet_actions_app['main']
    dialog = create_dialog(case, page)
    provider.fault = 'create_playlist'
    def receipt(route):
        actual = route.fetch()
        assert actual.status == 502
        route.fulfill(status=422, json={'detail': DIAGNOSTIC, 'code': 'unknown'})
    page.route('**/songs/create-playlist', receipt)
    dialog.get_by_role('button', name='Create playlist', exact=True).click()
    expect(dialog.get_by_text('This change was not confirmed. Review it before trying again.', exact=True)).to_be_visible()
    expect(dialog.get_by_label('Playlist name')).to_have_value('Keep this playlist draft')
    expect(dialog.get_by_role('button', name='Create playlist', exact=True)).to_be_enabled()
    assert provider.calls.count('create_playlist') == 1 and not provider.created
    assert_quiet_action(page, unconfirmed_song_action=True)
    page.screenshot(path=action_evidence + '-unrecognized-validation.png')


def test_known_account_validation_remains_useful_and_keeps_draft_for_explicit_retry(
        quiet_actions_app, page, action_evidence):
    case, provider = quiet_actions_app, quiet_actions_app['main']
    dialog = create_dialog(case, page)
    case['clients'].pop(case['main_id'])
    dialog.get_by_role('button', name='Create playlist', exact=True).click()
    expect(dialog.get_by_text('The required account is not connected', exact=True)).to_be_visible()
    expect(dialog.get_by_label('Playlist name')).to_have_value('Keep this playlist draft')
    expect(dialog.get_by_role('button', name='Create playlist', exact=True)).to_be_enabled()
    assert not provider.created and not provider.added
    page.screenshot(path=action_evidence + '-known-account-validation.png')
    case['clients'][case['main_id']] = provider
    dialog.get_by_role('button', name='Create playlist', exact=True).click()
    expect(dialog.get_by_text('Playlist created.', exact=True)).to_be_visible()
    assert len(provider.created) == 1 and len(provider.added) == 1
    assert_quiet_action(page)


def test_album_creation_failure_keeps_open_draft_through_quiet_get_reconciliation(
        quiet_actions_app, page, action_evidence):
    case, provider = quiet_actions_app, quiet_actions_app['main']
    page.set_viewport_size({'width': 1280, 'height': 900})
    url = case['base'] + '/album?browse=MPRE_new'
    page.goto(url)
    page.get_by_role('button', name='Create a playlist from this album', exact=True).click()
    form = page.locator('form[hx-post="/album/create-playlist"]')
    name = form.locator('input[name="name"]')
    name.fill('Retain my album playlist name')
    provider.fault, provider.kind, provider.after = 'create_playlist', 'json', True
    posts = []
    page.on('request', lambda request: posts.append(request.method)
            if request.url.endswith('/album/create-playlist') else None)
    with page.expect_response(lambda response: response.url == url and response.request.method == 'GET') as refreshed:
        with page.expect_response('**/album/create-playlist') as failed:
            form.get_by_role('button', name='Create playlist', exact=True).click()
        assert failed.value.status == 503
        expect(name).to_have_value('Retain my album playlist name')
        expect(form.get_by_role('button', name='Create playlist', exact=True)).to_be_enabled()
    assert refreshed.value.status == 200
    expect(form).to_be_visible()
    expect(name).to_have_value('Retain my album playlist name')
    expect(form.get_by_text('Creation was not confirmed. Check your playlists before creating again.', exact=True)).to_be_visible()
    expect(form.get_by_role('button', name='Create playlist', exact=True)).to_be_enabled()
    assert posts == ['POST'] and provider.calls.count('create_playlist') == 1
    assert len(provider.created) == 1 and not provider.added
    assert not [p for p in case['store'].playlists.get_playlists() if p.title == 'Retain my album playlist name']
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-album-create-retained-draft.png')
