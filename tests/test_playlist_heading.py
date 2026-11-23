"""Playlist headings and the rename response show the actual saved name."""
from html import unescape
import re
from fastapi.testclient import TestClient
import pytest

from tests.conftest import FakeClient
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('generated', [False, True])
@pytest.mark.parametrize('metadata', [False, True])
@pytest.mark.parametrize('title', [
    'More in your wheelhouse - November 9 2026 #143',
    'Radio Session · 2026-11-09 #2',
    'My "own" mix & favorites',
])
def test_saved_name_survives_rendering_and_rename(store, generated, metadata, title):
    account = store.accounts.upsert_account('Main', None, True)
    pid = store.playlists.upsert_playlist(account, 'PL1', title, 0, 'h', 1)
    if metadata:
        tid = store.tracks.upsert_track('song', 'Song', 'Artist')
        store.tracks.set_track_genre(tid, 'Space Rock')
        store.tracks.set_track_year(tid, '1994')
        store.playlists.set_playlist_tracks(pid, [tid])
    if generated:
        store.playlists.mark_generated_playlist('PL1')
    provider = FakeClient()
    client = TestClient(create_app(store, lambda: {account: provider}, now_fn=lambda: 1),
                        base_url='http://127.0.0.1')
    for response in (client.get(f'/playlist/{pid}'),
                     client.post(f'/playlist/{pid}/rename', data={'title': title})):
        assert response.status_code == 200
        heading = re.search(r'<div id="pl-title"[^>]* data-title="([^"]*)".*?<h1\b[^>]*>(.*?)</h1>',
                            response.text, re.S)
        assert heading is not None
        assert unescape(heading[1]) == title
        recognized = generated and title != 'My "own" mix & favorites'
        expected = title.split(' - November')[0].split(' · 2026')[0] if recognized else title
        if recognized and metadata:
            expected += ' with mostly space rock from the 1990s'
        assert unescape(heading[2].strip()) == expected
        assert ('class="pl-type-badge"' in response.text) == recognized
        assert ('class="pl-version"' in response.text) == recognized
        assert 'class="pl-title-context"' not in response.text
    assert store.playlists.get_playlist(pid).title == title


def test_sparse_track_metadata_does_not_name_the_mix():
    from yt_playlist.web.playlist_heading import title_context

    tracks = [{'genre': 'Space Rock', 'year': 1994}] + [{} for _ in range(5)]
    heading = title_context('From your catalog - November 12 2026 #1',
                            is_generated=True, tracks=tracks)['generated_heading']
    assert heading['name'] == 'From your catalog'
