"""Actual playlist actions preserve their controls through quiet GET recovery."""
import json
import inspect

import pytest
from playwright.sync_api import expect

from tests.browser.quiet_action_support import action_evidence, assert_quiet_action
from tests.browser.test_quiet_validation_browser import quiet_actions_app
from tests.conftest import _track
from tests.test_quiet_playlist_actions import diagnostic
from yt_playlist.util.action_kinds import ADD_TRACKS

pytestmark = pytest.mark.browser


def prepare_playlist(case, operation):
    store, provider, pid = case['store'], case['main'], case['source']
    tracks = [_track(vid, title, 'Artist & Friends', set_video_id='set-' + vid)
        for vid, title in [('owned-song', 'Owned Song'), ('next-song', 'Next Song'),
                           ('owned-live', 'Owned Song (Live)')]]
    provider._catalog.update({track['videoId']: track for track in tracks})
    provider._tracks['SOURCE'] = list(tracks[:2])
    provider._search_results = [tracks[2]]
    ids = [store.tracks.upsert_track(track['videoId'], track['title'], 'Artist & Friends', 'Album', 200)
           for track in tracks[:2]]
    if operation == 'retry-placement':
        provider._tracks['SOURCE'].append(tracks[2])
        ids.append(store.tracks.upsert_track('owned-live', 'Owned Song (Live)', 'Artist & Friends', 'Album', 200))
        case['placement_id'] = store.actions.record_action(ADD_TRACKS, '{}', json.dumps({
            'playlist_id': pid, 'after_video_id': 'owned-song', 'video_ids': ['owned-live']}),
            'pending', '{}', 100)
    store.playlists.set_playlist_tracks(pid, ids)
    store.playlists.set_playlist_track_count(pid, len(ids), 100)
    target = store.playlists.upsert_playlist(case['main_id'], 'COPY-DEST', 'Copy destination', 0, '', 100)
    provider._tracks['COPY-DEST'] = []
    case['copy_target'] = target


def open_action(case, page, operation):
    page.set_viewport_size({'width': 1440, 'height': 1000})
    url = case['base'] + ('/playlists' if operation == 'copy-into' else f"/playlist/{case['source']}")
    page.goto(url)
    page.evaluate('window.quiet879Document = document')
    if operation == 'copy-into':
        page.locator(f'tr[data-playlist-id="{case["source"]}"]:visible input[type=checkbox]').check()
        page.get_by_role('button', name='Copy into', exact=True).click()
        page.locator('select[x-model="copyIntoTarget"]').select_option(str(case['copy_target']))
        return url, lambda: page.get_by_role('button', name='Copy in', exact=True).click()
    if operation == 'rename':
        page.get_by_role('heading', name='Source mix', exact=True).click()
        page.locator('input.title-input').fill('Keep this rename draft')
        return url, lambda: page.locator('input.title-input').press('Enter')
    if operation == 'retry-placement':
        return url, lambda: page.get_by_role('button', name='Retry placement', exact=True).click()
    page.get_by_role('button', name='More actions for Owned Song', exact=True).click()
    if operation == 'add-tracks':
        page.get_by_role('menuitem', name='Find alternate versions', exact=True).click()
        dialog = page.get_by_role('dialog', name='Alternate versions', exact=True)
        expect(dialog.get_by_text('Owned Song (Live)', exact=True)).to_be_visible()
        dialog.locator('input[name="track"]').check()
        return url, lambda: dialog.get_by_role('button', name='Add to playlist', exact=True).click()
    page.get_by_role('menuitem', name='Remove from playlist', exact=True).click()
    return url, lambda: page.get_by_role('button', name='Remove', exact=True).click()


def assert_retained(case, page, operation):
    if operation == 'copy-into':
        expect(page.locator(f'tr[data-playlist-id="{case["source"]}"]:visible input[type=checkbox]')).to_be_checked()
        expect(page.locator('select[x-model="copyIntoTarget"]')).to_have_value(str(case['copy_target']))
        expect(page.get_by_role('button', name='Copy in', exact=True)).to_be_enabled()
        expect(page.get_by_text('Copy was not confirmed. Review the destination before copying again.', exact=True)).to_be_visible()
    elif operation == 'rename':
        expect(page.locator('input.title-input')).to_be_visible()
        expect(page.locator('input.title-input')).to_have_value('Keep this rename draft')
        expect(page.get_by_text('Rename was not confirmed. Review the playlist name before trying again.', exact=True)).to_be_visible()
    elif operation == 'retry-placement':
        expect(page.get_by_role('button', name='Retry placement', exact=True)).to_be_enabled()
        expect(page.get_by_text('Placement is still pending. Review the playlist before trying again.', exact=True)).to_be_visible()
    elif operation == 'add-tracks':
        dialog = page.get_by_role('dialog', name='Alternate versions', exact=True)
        expect(dialog).to_be_visible()
        expect(dialog.locator('input[name="track"]')).to_be_checked()
        expect(dialog.get_by_role('button', name='Add to playlist', exact=True)).to_be_enabled()
        expect(dialog.get_by_text('Addition was not confirmed. Review the playlist before adding again.', exact=True)).to_be_visible()
    else:
        expect(page.get_by_role('heading', name='Remove track?', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Remove', exact=True)).to_be_enabled()
        expect(page.get_by_text('Removal was not confirmed. Review the playlist before removing again.', exact=True)).to_be_visible()
        expect(page.locator('tr.srow[data-vid="owned-song"]')).to_be_visible()


@pytest.mark.parametrize('operation,kind', [
    (operation, kind) for operation in ['add-tracks', 'retry-placement', 'rename', 'remove-track', 'copy-into']
    for kind in ['value', 'json']] + [('retry-placement', 'runtime')])
def test_playlist_lookup_failure_keeps_action_state_through_actual_get_reconciliation(
        quiet_actions_app, page, monkeypatch, kind, operation, action_evidence, caplog):
    case = quiet_actions_app
    prepare_playlist(case, operation)
    url, submit = open_action(case, page, operation)
    path = '/playlists/copy-into' if operation == 'copy-into' else f"/playlist/{case['source']}/{operation}"
    requests, lookups = [], []
    page.on('request', lambda request: requests.append(request.method) if request.url.endswith(path) else None)
    before = case['store'].charts.playlist_tracks_detail(case['source'])
    playlists = case['store'].playlists.get_playlists()
    lookup = case['store'].playlists.get_playlist

    def fail(playlist_id):
        if not lookups and inspect.currentframe().f_back.f_globals.get('__name__') == 'yt_playlist.library.ops':
            lookups.append(playlist_id)
            diagnostic(kind)
        return lookup(playlist_id)

    with page.expect_response(lambda response: response.url == url and response.request.method == 'GET') as refreshed:
        with monkeypatch.context() as failing:
            failing.setattr(case['store'].playlists, 'get_playlist', fail)
            with page.expect_response('**' + path) as failed:
                submit()
            assert failed.value.status == 503 and failed.value.body() == b''
        assert_retained(case, page, operation)
        assert_quiet_action(page)
        page.screenshot(path=action_evidence + '-playlist-action-failure.png')
    assert refreshed.value.status == 200
    assert 'internal-diagnostic-879' in caplog.text
    assert_retained(case, page, operation)
    assert page.evaluate('window.quiet879Document === document')
    assert requests == ['POST']
    assert lookups == [case['copy_target'] if operation == 'copy-into' else case['source']]
    assert case['store'].charts.playlist_tracks_detail(case['source']) == before
    assert case['store'].playlists.get_playlists() == playlists
    provider = case['main']
    assert not provider.added and not provider.removed and not provider.edited and not provider.created
    assert_retained(case, page, operation)
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-playlist-action-reconciled.png')

    # Explicit retry runs the real operation once, using the retained action.
    with page.expect_response('**' + path) as retried:
        submit()
    assert retried.value.status == 200 and requests == ['POST', 'POST']
    if operation == 'rename':
        expect(page.get_by_role('heading', name='Keep this rename draft', exact=True)).to_be_visible()
        expect(page.get_by_text('Rename was not confirmed. Review the playlist name before trying again.', exact=True)).not_to_be_visible()
        assert len(provider.edited) == 1
    elif operation == 'remove-track':
        expect(page.locator('tr.srow[data-vid="owned-song"]')).to_have_count(0)
        assert len(provider.removed) == 1
    elif operation == 'retry-placement':
        assert case['store'].actions.get_action(case['placement_id']).status == 'executed'
        assert len(provider.edited) == 1 and not provider.added
        expect(page.get_by_role('button', name='Retry placement', exact=True)).not_to_be_visible()
    else:
        assert len(provider.added) == 1
        target = case['copy_target'] if operation == 'copy-into' else case['source']
        videos = [track['video_id'] for track in case['store'].charts.playlist_tracks_detail(target)]
        assert videos == (['owned-song', 'next-song'] if operation == 'copy-into' else
                          ['owned-song', 'owned-live', 'next-song'])
        expect(page.get_by_text('Addition was not confirmed. Review the playlist before adding again.', exact=True)).not_to_be_visible()
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-playlist-action-explicit-recovery.png')


@pytest.mark.parametrize('operation', ['add-tracks', 'remove-track', 'copy-into'])
def test_reconciliation_filters_removed_members_and_navigation_drops_action_state(
        quiet_actions_app, page, monkeypatch, operation, action_evidence):
    case = quiet_actions_app
    prepare_playlist(case, operation)
    url, submit = open_action(case, page, operation)
    path = '/playlists/copy-into' if operation == 'copy-into' else f"/playlist/{case['source']}/{operation}"
    lookup = case['store'].playlists.get_playlist
    failures, posts = [], []
    page.on('request', lambda request: posts.append(request.method) if request.url.endswith(path) else None)

    def fail_once(playlist_id):
        if not failures and inspect.currentframe().f_back.f_globals.get('__name__') == 'yt_playlist.library.ops':
            failures.append(playlist_id)
            diagnostic('value')
        return lookup(playlist_id)

    with page.expect_response(lambda response: response.url == url and response.request.method == 'GET'):
        with monkeypatch.context() as failing:
            failing.setattr(case['store'].playlists, 'get_playlist', fail_once)
            with page.expect_response('**' + path) as response:
                submit()
            assert response.value.status == 503
        if operation == 'copy-into':
            case['store'].playlists.remove_playlist(case['copy_target'])
        else:
            tid = case['store'].tracks.track_ids_for_videos(['next-song'])['next-song']
            case['store'].playlists.set_playlist_tracks(case['source'], [tid])
            case['store'].playlists.set_playlist_track_count(case['source'], 1, 101)
    if operation == 'copy-into':
        expect(page.locator('select[x-model="copyIntoTarget"]')).to_have_value('')
        expect(page.get_by_role('button', name='Copy in', exact=True)).to_be_disabled()
        expect(page.locator(f'tr[data-playlist-id="{case["source"]}"]:visible input[type=checkbox]')).to_be_checked()
    else:
        expect(page.get_by_role('dialog', name='Alternate versions', exact=True)).not_to_be_visible()
        expect(page.get_by_role('heading', name='Remove track?', exact=True)).not_to_be_visible()
        expect(page.locator('tr.srow[data-vid="owned-song"]')).to_have_count(0)
    assert posts == ['POST']
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-removed-action-target.png')
    # Real same-document navigation gets no recovery-state object.
    page.evaluate("navigation.navigate('/artists')")
    expect(page.get_by_role('heading', name='Artists', exact=True)).to_be_visible()
    page.evaluate('(url) => navigation.navigate(url)', url)
    expect(page.get_by_role('heading', name='Playlists' if operation == 'copy-into' else 'Source mix', exact=True)).to_be_visible()
    expect(page.get_by_role('dialog', name='Alternate versions', exact=True)).not_to_be_visible()
    expect(page.get_by_role('heading', name='Remove track?', exact=True)).not_to_be_visible()
    expect(page.locator('select[x-model="copyIntoTarget"]')).not_to_be_visible()
    assert posts == ['POST']
    assert page.evaluate('window.quiet879Document === document')
    assert not case['main'].added and not case['main'].removed and not case['main'].edited


@pytest.mark.parametrize('operation', ['add-tracks', 'rename', 'remove-track', 'copy-into'])
def test_completed_real_mutation_with_lost_receipt_is_only_reconciled_with_get(
        quiet_actions_app, page, operation, action_evidence):
    case = quiet_actions_app
    prepare_playlist(case, operation)
    url, submit = open_action(case, page, operation)
    path = '/playlists/copy-into' if operation == 'copy-into' else f"/playlist/{case['source']}/{operation}"
    posts, completed = [], []
    page.on('request', lambda request: posts.append(request.method) if request.url.endswith(path) else None)

    def lose_receipt(route):
        actual = route.fetch()
        completed.append(actual.status)
        assert actual.status == 200
        route.fulfill(status=503, headers={'HX-Reswap': 'none'}, body='')

    page.route('**' + path, lose_receipt)
    with page.expect_response(lambda response: response.url == url and response.request.method == 'GET') as refreshed:
        with page.expect_response('**' + path) as failed:
            submit()
        assert failed.value.status == 503
    assert refreshed.value.status == 200 and completed == [200] and posts == ['POST']
    assert page.evaluate('window.quiet879Document === document')
    provider, store = case['main'], case['store']
    if operation == 'rename':
        expect(page.get_by_role('heading', name='Keep this rename draft', exact=True)).to_be_visible()
        expect(page.locator('input.title-input')).not_to_be_visible()
        assert store.playlists.get_playlist(case['source']).title == 'Keep this rename draft'
        assert len(provider.edited) == 1
    elif operation == 'remove-track':
        expect(page.locator('tr.srow[data-vid="owned-song"]')).to_have_count(0)
        expect(page.get_by_role('heading', name='Remove track?', exact=True)).not_to_be_visible()
        assert len(provider.removed) == 1
    else:
        assert_retained(case, page, operation)
        target = case['copy_target'] if operation == 'copy-into' else case['source']
        videos = [track['video_id'] for track in store.charts.playlist_tracks_detail(target)]
        assert videos == (['owned-song', 'next-song'] if operation == 'copy-into' else
                          ['owned-song', 'owned-live', 'next-song'])
        assert len(provider.added) == 1
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-real-mutation-lost-receipt.png')
    page.reload()
    assert completed == [200] and posts == ['POST']
    assert len(provider.added) == int(operation in ['add-tracks', 'copy-into'])
    assert len(provider.removed) == int(operation == 'remove-track')
    assert len(provider.edited) == int(operation in ['add-tracks', 'rename'])
