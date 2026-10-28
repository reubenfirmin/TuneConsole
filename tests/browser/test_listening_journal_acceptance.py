"""Opt-in visual acceptance against a read-only copy of a saved listening library."""
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import time

from playwright.sync_api import expect
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import listening_analytics
from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo


@pytest.fixture
def store(tmp_path, monkeypatch, request):
    source=request.config.getoption('--listening-acceptance-db') or os.environ.get('YT_PLAYLIST_LISTENING_ACCEPTANCE_DB')
    if not source:
        pytest.skip('Set YT_PLAYLIST_LISTENING_ACCEPTANCE_DB for saved-library acceptance')
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS','1')
    destination=tmp_path/'listening-acceptance.db'

    def remove_copy():
        for suffix in ('','-journal','-wal','-shm'):
            Path(str(destination)+suffix).unlink(missing_ok=True)

    # Saved libraries can be several GB. Release each copy before the next
    # viewport case, including when setup or a browser assertion fails.
    request.addfinalizer(remove_copy)
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer)
    copy=Store(destination)
    request.addfinalizer(copy.conn.close)
    copy.init_schema()
    started=time.monotonic()
    assert listening_analytics.build(copy,time.time())
    repo=ListeningAnalyticsRepo(copy)
    manifest,edition=repo.prepared(),repo.prepared('listening_edition')
    evidence={'manifest_bytes':len(repo.encoded().encode()),'chunks':len(manifest['chunks']),
              'largest_chunk_bytes':max((c['bytes'] for c in manifest['chunks']),default=0),
              'build_seconds':round(time.monotonic()-started,3),
              'stories':[{k:s[k] for k in ('family','title','evidence','url')} for s in edition['stories']]}
    Path('/tmp/tuneconsole-listening-acceptance.json').write_text(json.dumps(evidence,indent=2))
    assert evidence['manifest_bytes']<32768
    assert evidence['largest_chunk_bytes']<512*1024
    return copy


@pytest.mark.parametrize('width',[1440,390])
def test_saved_library_journal_and_analytics(live_app,store,page,width):
    errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
    page.set_viewport_size({'width':width,'height':1100})
    edition=ListeningAnalyticsRepo(store).prepared('listening_edition')
    if edition['available']:
        page.goto(live_app+'/charts')
        expect(page.locator('.le-lead')).to_be_visible()
        expect(page.locator('[data-listening-edition] figcaption')).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert page.locator('[data-listening-edition] img, [data-listening-edition] image').evaluate_all(
            "els => els.every(el => el.closest('.le-song-play, .le-mini-play, .le-backdrop'))")
        assert page.locator('.le-song-play img').evaluate_all(
            "images => images.every(image => image.referrerPolicy==='no-referrer' && getComputedStyle(image).borderRadius==='8px')")
        page.screenshot(path=f'/tmp/tuneconsole-listening-saved-editorial-{width}.png',full_page=True,animations='disabled')
        page.locator('h2 .le-story-link').first.click()
    else:
        page.goto(live_app+'/charts?view=analytics&window=30d')
    expect(page.locator('.lp-summary')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.locator('[data-listening-analytics]').screenshot(path=f'/tmp/tuneconsole-listening-saved-charts-{width}.png')
    assert not errors,errors
