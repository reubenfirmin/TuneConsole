"""Server and live browser navigation must obey the same identity contract."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from yt_playlist.util.navigation import LINK_CONFIG, album_url, artist_url, playlist_url, track_url


def test_detail_link_identity_and_encoding():
    assert artist_url(7, browse='UC_wrong') == '/artist?artist_id=7'
    assert artist_url(browse='UC_actual') == '/artist?browse=UC_actual'
    assert artist_url() is None
    assert playlist_url(9, account_id=2, provider_id='remote') == '/playlist/9'
    assert playlist_url(account_id=2, provider_id='remote/a+b') == '/playlist/saved/2/remote%2Fa%2Bb'
    assert playlist_url(provider_id='remote') is None
    assert album_url('album/a+b&c') == '/album?browse=album%2Fa%2Bb%26c'
    assert track_url({'video_id': 'upload/a+b', 'title': 'A & B', 'artist_browse_id': 'UC_actual'}, page=0) == (
        '/track/upload%2Fa%2Bb?title=A+%26+B&artist_browse=UC_actual&page=0')


def test_browser_and_server_detail_links_agree():
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is required for browser navigation contracts')
    song = {'video_id': 'upload/a+b', 'title': "A & B's ~ (!*) café", 'duration': 0,
            'album_browse': '', 'album_browse_id': 'album/+&', 'artist_browse_id': 'UC_actual'}
    calls = [
        ['artistUrl', [7, 'UC_wrong']], ['artistUrl', [None, 'UC_actual']], ['artistUrl', []],
        ['albumUrl', ['album/a+b&c']], ['albumUrl', [None]],
        ['playlistUrl', [9, 2, 'remote']], ['playlistUrl', [None, 2, 'remote/a+b']],
        ['playlistUrl', [None, None, 'remote']], ['playlistUrl', []],
        ['trackUrl', [song]], ['trackUrl', [song, 0]], ['trackUrl', ['upload/a+b']],
        ['trackUrl', [{'videoId': 'external', 'artist': 'Outside'}]],
        ['trackUrl', [{}]],
    ]
    expected = [artist_url(7, browse='UC_wrong'), artist_url(browse='UC_actual'), artist_url(),
        album_url('album/a+b&c'), album_url(None),
        playlist_url(9, account_id=2, provider_id='remote'),
        playlist_url(account_id=2, provider_id='remote/a+b'), playlist_url(provider_id='remote'), playlist_url(),
        track_url(song), track_url(song, page=0), track_url('upload/a+b'),
        track_url({'videoId': 'external', 'artist': 'Outside'}), track_url({})]
    source = Path(__file__).parents[1] / 'src/yt_playlist/web/static/detail-links.js'
    runner = """
        const fs = require('node:fs'), vm = require('node:vm');
        const input = JSON.parse(fs.readFileSync(0, 'utf8'));
        const context = vm.createContext({window: {}, document: {
            getElementById: () => ({textContent: JSON.stringify(input.config)})
        }});
        vm.runInContext(fs.readFileSync(input.source, 'utf8'), context);
        console.log(JSON.stringify(input.calls.map(([name, args]) => context.window[name](...args))));
    """
    result = subprocess.run([node, '-e', runner], input=json.dumps({
        'source': str(source), 'config': LINK_CONFIG, 'calls': calls}, sort_keys=True),
        text=True, capture_output=True, timeout=10, check=True)
    assert json.loads(result.stdout) == expected
