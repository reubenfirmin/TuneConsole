"""Confirmed saves use current track facts before local import can write."""
import json

import pytest
from playwright.sync_api import expect

from tests.browser.test_generated_launch_browser import generation_app
from yt_playlist.web.jobs import SyncJobs


@pytest.mark.parametrize('metadata,vibe', [('complete', None), ('missing', None), ('complete', 'release')])
def test_confirmed_save_and_reopened_receipt_use_the_imported_heading(
        generation_app, store, page, monkeypatch, metadata, vibe):
    base, provider, *_ = generation_app
    rows = store.conn.execute(
        "SELECT id,video_id,title,artist FROM catalog_tracks WHERE video_id IN ('one','two')").fetchall()
    assert len(rows) == 2
    if metadata == 'complete':
        for row in rows:
            store.tracks.set_track_genre(row['id'], 'Drum & Bass')
            store.tracks.set_track_year(row['id'], '2014')
    tracks = [{key: row[key] for key in ('video_id', 'title', 'artist')} for row in rows]
    recipe = {'lane': 'explore', **({'vibe': vibe} if vibe else {})}
    original = 'From your catalog' + (' - Let off steam' if vibe else '') + ' - October 31 2026'
    get_playlist = provider.get_playlist
    reads = []

    def detail_with_provider_title(ytm, **kwargs):
        reads.append(ytm)
        detail = get_playlist(ytm, **kwargs)
        saved = next((entry for entry in provider.created if entry[0] == ytm), None)
        return {**detail, **({'title': saved[1]} if saved else {})}

    monkeypatch.setattr(provider, 'get_playlist', detail_with_provider_title)
    store.activity_paused = True
    store._evidence_maintenance = 1
    store.conn.execute('PRAGMA query_only=ON')
    try:
        response = page.request.post(base + '/home/generate', headers={'HX-Request': 'true'},
            form={'name': original, 'tracks': json.dumps(tracks), 'recipe': json.dumps(recipe)})
        assert response.status == 200
        destination = response.headers['hx-redirect']
        assert destination.startswith('/playlist/saved/')
        reads.clear()
        expected = 'Rediscover your drum & bass' if metadata == 'complete' else 'Worth another listen'
        for reopen in (False, True):
            if reopen:
                page.reload()
            else:
                page.goto(base + destination)
            expect(page.locator('.editable-title')).to_have_text(expected)
            expect(page.locator('#playlist-track-body .srow')).to_have_count(2)
            expect(page.locator('.pl-title-context')).to_have_count(int(metadata == 'complete'))
            if metadata == 'complete':
                expect(page.locator('.pl-title-context')).to_have_text('From the 2010s')
            expect(page.locator('.pl-headmain')).to_contain_text('From your catalog')
            expect(page.locator('.pl-headmain')).to_contain_text('October 31, 2026')
            if vibe:
                expect(page.locator('.pl-headmain')).to_contain_text('Let off steam')
        assert not reads, 'A retained confirmed receipt needs no provider reread'
        if not vibe:
            # Receipt expiry/restart falls back to a complete provider read; it
            # still uses the same detached local genre/year facts without writes.
            latest = SyncJobs.latest
            monkeypatch.setattr(SyncJobs, 'latest', lambda self, kind, params:
                                None if kind == 'generated_playlist' else latest(self, kind, params))
            page.reload()
            expect(page.locator('.editable-title')).to_have_text(expected)
            assert reads == [provider.created[0][0]]
            expect(page.locator('.pl-title-context')).to_have_count(int(metadata == 'complete'))
        page.set_viewport_size({'width': 1280, 'height': 800})
        page.screenshot(path=f'/tmp/tuneconsole-880-saved-{metadata}-{vibe or "plain"}.png')
        assert len(provider.created) == 1
        assert provider.created[0][1] == original + ' #1'
        assert len(store.playlists.get_playlists()) == 1
        assert not provider.edited
    finally:
        store.conn.execute('PRAGMA query_only=OFF')
        store.activity_paused = False
        store._evidence_maintenance = 0
