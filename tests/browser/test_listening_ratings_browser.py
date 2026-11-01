"""Real ratings and frozen start observations reach the main Charts stories."""
import json
import re

import pytest
from playwright.sync_api import expect

from tests.conftest import seed_catalog_track
from tests.rec.test_listening_editions import NOW
from yt_playlist.library import ratings
from yt_playlist.rec import listening_analytics


@pytest.fixture(autouse=True)
def listening_enabled(monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS', '1')


@pytest.mark.parametrize('width', [390, 1440])
def test_confirmed_choices_show_songs_and_reconcilable_totals(store, live_app, page, width, tmp_path):
    from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo

    iid = store.accounts.upsert_account('Listener', 'cred', None, True)
    store.settings.set_setting('listening_timezone', 'UTC')
    for i in range(8):
        video = f'choicevid{i}'
        seed_catalog_track(store, f'choice-{i}', f'Song {i}', f'Artist {i}', video_id=video)
        at = NOW - (i % 3) * 86400
        ratings.record_confirmed(store, iid, video, 'LIKE', at-200)
        ratings.record_confirmed(store, iid, video, 'DISLIKE', at-100)
        ratings.record_confirmed(store, iid, video, 'INDIFFERENT', at)
    listening_analytics.build(store, NOW)
    story = ListeningAnalyticsRepo(store).prepared('listening_edition')['stories'][0]
    assert story['family'] == 'rating_actions'
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1100})
    page.goto(live_app+'/charts')
    panel = page.locator('.le-rating_actions')
    expect(panel.locator('.le-evidence')).to_contain_text('24 confirmed rating changes across 8 songs')
    expect(panel.locator('.le-format-key span')).to_have_text(['Hearts 8', 'Skulls 8', 'Clears 8'])
    table = panel.get_by_role('table', name='Songs with confirmed rating changes')
    expect(table.locator('tbody tr')).to_have_count(7)
    expect(panel.locator('.le-rating-caption')).to_have_text('Latest 6 of 8 songs')
    expect(table.locator('tbody tr').last.locator('th')).to_have_text('2 other songs')
    expect(table.locator('tbody tr').last.locator('td')).to_have_text(['2', '2', '2'])
    for row in table.locator('tbody tr').all()[:-1]:
        expect(row.locator('a')).to_have_attribute('href', re.compile(r'https://music.youtube.com/watch\?v=choicevid\d'))
        assert [cell.inner_text() for cell in row.locator('td').all()] == ['1', '1', '1']
    expect(panel.locator('.le-svg-count').first).to_have_text('Actions')
    page.evaluate('document.fonts.ready')
    headers = table.locator('thead th')
    expect(headers).to_have_text(['Song', 'Hearts', 'Skulls', 'Clears'])
    header_bounds = headers.evaluate_all('''headers => headers.map(header => {
        const range = document.createRange();
        range.selectNodeContents(header);
        const text = range.getBoundingClientRect();
        const cell = header.getBoundingClientRect();
        return {label: header.textContent, left: text.left, right: text.right,
                cellLeft: cell.left, cellRight: cell.right};
    })''')
    (tmp_path / f'choices-header-layout-{width}.json').write_text(
        json.dumps(header_bounds, indent=2) + '\n')
    for header in header_bounds:
        assert header['left'] >= header['cellLeft'] - .5, header_bounds
        assert header['right'] <= header['cellRight'] + .5, header_bounds
    for left, right in zip(header_bounds, header_bounds[1:]):
        assert right['left'] - left['right'] >= 4, header_bounds
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / f'choices-{width}.png'), full_page=True)
    assert not errors


@pytest.mark.parametrize('width', [1440, 390])
def test_current_genre_preferences_and_known_start_share_are_visible(store, live_app, page, width):
    iid = store.accounts.upsert_account('Listener', 'cred', None, True)
    store.settings.set_setting('listening_timezone', 'UTC')
    for i in range(9):
        seed_catalog_track(store, f'rating-{i}', f'Song {i}', f'Artist {i}', video_id=f'ratingvid{i}',
                           genre='Jazz' if i < 5 else 'House')
        ratings.record_observations(store, iid, {f'ratingvid{i}': 'LIKE'}, source='sync', now=NOW-5000)
    for i in range(36):
        at = NOW-(i%3)*86400-i*100
        store.history.record_play_event(iid, 'rating-0', 'ratingvid0', at, playlist_ytm_id='LM')
        if i < 27:
            store.player_events.record_player_event(iid, 'listen_start', 'ratingvid0', None, None, 'LM',
                json.dumps({'rating': 'LIKE' if i < 18 else 'INDIFFERENT'}), at)
    # Today's reversal cannot change the eighteen earlier LIKE start observations.
    ratings.record_confirmed(store, iid, 'ratingvid0', 'DISLIKE', NOW-20)
    payload = listening_analytics.build(store, NOW)
    assert payload['retained']['hearts'] == 8
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1100})
    page.goto(live_app + '/charts')
    collection = page.locator('.le-rating_genres')
    expect(collection.get_by_role('heading')).to_have_text('Your hearts span 2 genres.')
    expect(collection.locator('svg')).to_be_visible()
    expect(collection.locator('.le-rating-heart')).to_have_count(2)
    expect(collection.locator('.le-rating-skull')).to_have_count(2)
    share = page.locator('.le-rating_share')
    expect(share.locator('.le-orbit-number')).to_have_text('67%')
    expect(share.locator('.le-evidence')).to_contain_text('27 of 36 plays; 9 remain unknown')
    expect(share.locator('.le-format-key')).to_contain_text('Unknown at start 9')
    page.evaluate('document.fonts.ready')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=f'/tmp/tuneconsole-383-ratings-{width}.png', full_page=True)
    assert not errors


def test_qualified_reading_rotates_to_a_different_analysis_without_moving_the_current_page(store, page, monkeypatch, request):
    from yt_playlist.web import app as webapp
    from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo

    clock = [NOW]
    create = webapp.create_app

    def create_clocked(*args, **kwargs):
        return create(*args, **{**kwargs, 'now_fn': lambda: clock[0]})

    monkeypatch.setattr(webapp, 'create_app', create_clocked)
    live_app = request.getfixturevalue('live_app')
    from yt_playlist.rec.choice_capture import CaptureQueue
    captures = []
    store.choice_capture = CaptureQueue(lambda capture: captures.append(capture) or True)
    store.settings.set_setting('listening_timezone', 'UTC')
    for name in ('Ada', 'Bea', 'Cleo'):
        seed_catalog_track(store, name+'old', name+' Old song', name, video_id=name+'old')
        store.history.record_play_event(1, name+'old', name+'old', NOW-120*86400)
        store.history.record_play_event(1, name+'old', name+'old', NOW-86400)
        for i in range(5):
            key = name+str(i)
            seed_catalog_track(store, key, name+' New song '+str(i), name, video_id=key)
            store.history.record_play_event(1, key, key, NOW-(2+i%3)*86400-i*100)
    listening_analytics.build(store, NOW)
    repo = ListeningAnalyticsRepo(store)
    first = repo.prepared('listening_edition')
    assert first['stories'][0]['family'] == 'deeper'
    assert len({s['family'] for s in first['stories']}) == len(first['stories'])
    page.goto(live_app+'/charts')
    lead = page.locator('.le-lead h2')
    expect(lead).to_have_text(first['stories'][0]['title'])
    page.wait_for_timeout(1800)  # Actual visible dwell, not a direct impression POST.
    for capture in captures:
        capture.run()
    with store.read_snapshot() as snapshot:
        assert 'family:deeper' in ListeningAnalyticsRepo(snapshot.store).impressions()
    seed_catalog_track(store, 'rating', 'Other song', 'Other artist', video_id='rating')
    for i in range(90):
        at = NOW-(i%3)*86400-i*100
        store.history.record_play_event(1, 'rating', 'rating', at, playlist_ytm_id='LM')
        store.player_events.record_player_event(1, 'listen_start', 'rating', None, None, 'LM',
            json.dumps({'rating': 'LIKE' if i < 60 else 'INDIFFERENT'}), at)
    clock[0] = NOW+3600
    listening_analytics.build(store, clock[0])
    assert repo.prepared('listening_edition')['id'] == first['id']
    clock[0] = NOW+21601
    listening_analytics.build(store, clock[0])
    following = repo.prepared('listening_edition')
    assert following['stories'][0]['family'] == 'liked_starts'
    assert any(s['family'] == 'deeper' for s in following['stories'])
    expect(lead).to_have_text(first['stories'][0]['title'])
    page.reload()
    expect(lead).to_have_text('How often you return to hearted songs.')


@pytest.mark.parametrize('width', [1440, 390])
def test_sparse_rating_capture_keeps_four_useful_visible_analyses(store, live_app, page, width):
    from tests.rec.test_listening_editions import Repo
    from tests.rec.test_listening_ratings import mixed_evidence
    from yt_playlist.rec import listening_editions
    from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo

    payload = mixed_evidence()
    edition = listening_editions.prepare(payload, Repo(), NOW)
    repo = ListeningAnalyticsRepo(store)
    chunks, manifest = listening_analytics.partition({**payload, 'schema': 2, 'built_at': NOW})
    from yt_playlist.core import listening_outputs
    if store._listening_files:
        plan = listening_outputs.prepare(chunks, json.dumps(manifest), json.dumps(edition), NOW)
        with store.conn.atomic():
            listening_outputs.publish(store.conn, plan)
    else:
        repo.publish(json.dumps(edition), NOW, 'listening_edition')
        repo.publish(json.dumps(manifest), NOW)
        for key, chunk in chunks.items():
            repo.publish(chunk, NOW, 'listening_chunk:'+key)
        store.conn.commit()
    page.set_viewport_size({'width': width, 'height': 1100})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app+'/charts?view=home')
    expect(page.locator('.le-story')).to_have_count(4)
    for story in edition['stories']:
        panel = page.locator(f'[data-story-id="{story["id"]}"]')
        panel.scroll_into_view_if_needed()
        expect(panel).to_be_visible()
        expect(panel.locator('.le-composition > svg')).to_be_visible()
    assert page.locator('.le-rating_share, .le-decisions').count() == 0
    expect(page.locator('.le-rating_genres .le-evidence')).to_contain_text('2 hearts and 6 skulls')
    assert page.locator('.le-rating_genres .le-composition > svg').evaluate('''svg => {
        const headers=[...svg.querySelectorAll(':scope > text')].slice(0,2).map(el=>el.getBBox());
        return headers[0].x+headers[0].width<headers[1].x &&
            [...svg.querySelectorAll(':scope > g')].every(row=>{
                const name=row.querySelector('.le-svg-name').getBBox();
                const counts=row.querySelector('.le-svg-count').getBBox();
                return name.x+name.width<250 && counts.x>560 && counts.x+counts.width<=720;
            });
    }''')
    expect(page.locator('.le-discovery .le-evidence')).to_contain_text('10 of 279 plays')
    assert page.locator('.le-discovery circle').count() == 10
    assert page.locator('.le-composition > svg').evaluate_all(
        "els => els.every(el => !/NaN|Infinity/.test(el.innerHTML))")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=f'/tmp/tuneconsole-ready-variety-{width}.png', full_page=True)
    assert not errors
