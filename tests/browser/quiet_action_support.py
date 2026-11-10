"""Controlled transport failures and retained evidence for quiet action results."""
import hashlib
import json
from pathlib import Path
import socket
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.live_server import LiveServer
from tests.quiet_validation_support import prepare_quiet_actions


@pytest.fixture
def quiet_actions_app(store, monkeypatch):
    data = prepare_quiet_actions(store, monkeypatch)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(data['app'], host='127.0.0.1',
            port=listener.getsockname()[1], log_level='warning'))
        with LiveServer(server, sockets=[listener]):
            yield {**data, 'base': f'http://127.0.0.1:{listener.getsockname()[1]}'}


DIAGNOSTIC = 'internal-diagnostic-879: HTTP 503 transport/parser failure'


def fail_action(route, kind):
    if kind == 'network':
        route.abort()
    elif kind == 'plaintext':
        route.fulfill(status=503, content_type='text/plain', body=DIAGNOSTIC)
    elif kind == 'malformed':
        route.fulfill(content_type='application/json', body=DIAGNOSTIC)
    else:
        route.fulfill(status=503, json={'detail': DIAGNOSTIC, 'error': DIAGNOSTIC})


def assert_quiet_action(page, *, unconfirmed_song_action=False):
    expect(page.locator('body')).not_to_contain_text('internal-diagnostic-879')
    expect(page.locator('body')).not_to_contain_text('Unexpected token')
    expect(page.locator('#navigation-error, #playback-error, .toast-err, [role=alertdialog]:visible')).to_have_count(0)
    if unconfirmed_song_action:
        # Only this named action's fixed human outcome is allowed. An extra
        # status, arbitrary response text or the same text elsewhere still fails.
        dialog = page.locator('dialog.song-playlist-dialog[open]')
        expect(dialog).to_have_attribute('aria-labelledby', 'song-playlist-title')
        status = dialog.locator('form .song-action-error[role=status]')
        expect(status).to_be_visible()
        expect(status).to_have_text('This change was not confirmed. Review it before trying again.')
        expect(page.locator('[role=alert]:visible')).to_have_count(0)
    else:
        expect(page.locator('[role=alert]:visible')).to_have_count(0)


@pytest.fixture
def action_evidence(page, request):
    root = Path(__file__).resolve().parents[2]
    web = root / 'src/yt_playlist/web'
    names = ('track-detail.js', 'artist-start.js', 'album-choices.js', 'app-update.js',
             'account-growth.js', 'tv.js', 'song-actions.js', 'genre-dialog.js', 'road-trip.js',
             'enrichment-panel.js', 'library-playlists.js', 'table-controls.js',
             'app.js', 'app-navigation.js', 'read-recovery.js', 'home.js', 'playback-status.js',
             'track-detail.css', 'artist-start.css', 'album-choices.css', 'components.css')
    assets = {name: (web / 'static' / name).read_bytes() for name in names}
    files = [web / 'static' / name for name in names] + [web / 'templates' / name for name in (
        'track.html', 'artist_start.html', '_partials/album_tuner.html', '_partials/alerts.html',
        '_partials/growth_home_choice.html', '_partials/tv.html', '_partials/genre_candidates.html',
        'actions.html', '_partials/road_trip_draft.html', 'album.html', 'playlist.html',
        '_partials/move_panel.html', '_partials/move_row.html', '_album_actions.html',
        '_partials/liked_cell.html', '_partials/error_toast.html', '_partials/song_actions.html',
        '_partials/alternates_results.html', '_partials/pool_card.html',
        'playlists.html', '_partials/playlist_head.html', 'home.html', 'cleanup.html',
        '_partials/merge_body.html', '_partials/empty_row.html', '_partials/action_outcome.html')]
    files.append(web / 'routes/merge.py')
    hashes = lambda: {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    report = {'test': request.node.nodeid, 'source_before': hashes(), 'served': {}}
    stem = '/tmp/tuneconsole-879-actions-' + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:12]

    def served(response):
        name = urlsplit(response.url).path.removeprefix('/static/')
        if name in assets:
            report['served'].setdefault(name, []).append({
                'status': response.status, 'sha256': hashlib.sha256(response.body()).hexdigest()})

    page.on('response', served)
    try:
        yield stem
    finally:
        page.remove_listener('response', served)
        report['source_after'] = hashes()
        Path(stem + '.json').write_text(json.dumps(report, indent=2))
        assert report['source_before'] == report['source_after']
        assert report['served'], 'No implementation assets were observed'
        for name, responses in report['served'].items():
            assert all(item == {'status': 200, 'sha256': hashlib.sha256(assets[name]).hexdigest()}
                       for item in responses)
