"""The Charts home and BI views share facts, with separate compositions."""
import json
import math
import re

from playwright.sync_api import expect
import pytest

from tests.conftest import seed_catalog_track
from tests.test_mode_cards_route import prepare_home
from yt_playlist.rec import listening_analytics
from yt_playlist.library import ratings

NOW = 1792843200.0  # 2026-10-24 UTC


@pytest.fixture(autouse=True)
def listening_enabled(monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS','1')


def prepare(store):
    store.settings.set_setting('listening_timezone','UTC')
    artists = ['Artist A','Artist B','Artist C','Boards of Canada','Floating Points','Burial',
               'Nils Frahm','Bicep','Four Tet','Jon Hopkins','Autechre','Caribou',
               'The Comet Is Coming','Portico Quartet','Nala Sinephro','Kelly Lee Owens','Skee Mask','Overmono']
    for i, artist in enumerate(artists):
        for song in range(8):
            seed_catalog_track(store, f'song-{i}-{song}', f'Song {song}', artist,
                video_id=f'video-{i}-{song}', genre='Jazz' if i%3 == 1 else 'Techno')
    store.playlists.mark_generated_playlist('PLZ')
    store.settings.set_setting('radio_session_playlist_ids', '["radio"]')
    for ago in range(84):
        for i in range(len(artists)):
            count = max(0,round(2 + 2*math.sin(ago/(4+i%5) + i) - (ago%7 == 0)))
            for j in range(count):
                song = min(7,(83-ago)//(6+i%7))
                at = NOW-ago*86400-i*1000-j*100
                playlist = ['PLZ', 'OLAK5uy_album', 'radio', 'LM'][(ago+i) % 4]
                video = f'video-{i}-{song}'
                store.history.record_play_event(1,f'song-{i}-{song}',video,at,playlist_ytm_id=playlist)
                store.player_events.record_player_event(1,'listen_start',video,None,None,playlist,
                    json.dumps({'rating':'LIKE' if ago%3 else 'INDIFFERENT'}),at)
                for offset in (0,30):
                    store.player_events.record_player_event(1,'tick',video,offset,200,playlist,'{}',at+offset)
    ratings.record_confirmed(store,1,'video-0-0','LIKE',NOW-86400)
    ratings.record_confirmed(store,1,'video-1-0','DISLIKE',NOW-86400)
    assert listening_analytics.build(store,NOW+60)
    store.settings.set_setting('last_sync_at','1')
    store.settings.set_setting('onboard_dismissed','1')
    prepare_home(store,1)
    from tests.onboarding_support import publish_personal_mix
    publish_personal_mix(store,1)


@pytest.mark.parametrize('width', [1440,390])
def test_charts_home_and_analytics_filtering_and_shared_styles(live_app,store,page,width):
    prepare(store)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/bridge/status',lambda route:route.fulfill(json={'connected':True}))
    page.set_viewport_size({'width':width,'height':1100})
    page.goto(live_app + '/charts')
    panel = page.get_by_role('region',name='Listening journal',exact=True)
    expect(panel.locator('.le-lead')).to_be_visible()
    assert 1 <= panel.locator('.le-story').count() <= 4
    assert panel.locator('.le-composition svg, .le-bands').count() > 0
    assert panel.locator('.le-art, .le-description, .le-source').count() == 0
    assert panel.locator('img, image').evaluate_all("els => els.every(el => el.closest('.le-song-play, .le-mini-play, .le-backdrop'))")
    assert panel.locator('.le-copy').first.locator(':scope > :first-child').evaluate('el => el.tagName') == 'H2'
    assert panel.locator('.le-composition path').count() > 20
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.locator('body *').evaluate_all(
        'els => els.filter(e => e.getBoundingClientRect().right > innerWidth + 1).slice(0,8).map(e => e.className)')
    typography = panel.locator('h2').first.evaluate('(el) => getComputedStyle(el).fontFamily')
    assert 'Georgia' not in typography and 'Palatino' not in typography
    assert panel.locator('.lp-controls').count() == 0
    assert panel.locator('.le-masthead, .le-open, figcaption').count() == 0
    story_link = panel.locator('h2 .le-story-link').first.get_attribute('href')
    assert 'start=' in story_link and 'end=' in story_link
    play=panel.locator('a.le-song-play, a.le-mini-play').first
    expect(play).to_have_attribute('href', re.compile(r'https://music.youtube.com/watch\?v=video-'))
    expect(play).to_have_attribute('target','_blank')
    page.evaluate('document.fonts.ready')
    icon=play.locator('.le-play-icon, svg')
    expect(icon).to_have_css('opacity','0')
    play.hover()
    expect(icon).to_have_css('opacity','1')
    page.mouse.move(0,0)
    expect(icon).to_have_css('opacity','0')
    assert panel.locator('.le-song-thread').evaluate_all('''rows => rows.every(row => {
        const sleeve=row.querySelector('.le-song-sleeve'), trace=row.querySelector('.le-thread-fill').getBBox();
        if (!sleeve) return true;
        const box=sleeve.getBBox();
        return Math.abs(box.y+box.height-trace.y-trace.height)<.1 && box.x+box.width<trace.x;
    })''')
    panel.screenshot(path=f'/tmp/tuneconsole-listening-editorial-{width}.png',animations='disabled')
    page.locator('.chart-views').get_by_role('button',name='Listening analytics',exact=True).click()
    charts = page.locator('[data-listening-analytics]')
    expect(charts.locator('.lp-summary')).to_be_visible()
    expect(charts.locator('.lp-card:visible')).to_have_count(5)
    discovery = charts.get_by_role('region', name='Discovery rates')
    expect(discovery.locator('.lp-discovery-strip')).to_have_count(3)
    assert discovery.locator('.lp-discovery-line').evaluate_all('''paths => paths.every(path =>
        path.getAttribute('d').startsWith('M') && !/NaN|Infinity/.test(path.getAttribute('d')))''')
    assert discovery.locator('.lp-discovery-dot').count() > 0
    charts.locator('.lp-filter-disclosure summary').click()
    charts.get_by_label('Filter artist',exact=True).select_option(label='Artist A')
    expect(charts.locator('.lp-history-row')).to_have_count(1)
    expect(charts.locator('.lp-history-row a')).to_have_text('Artist A')
    charts.get_by_role('button',name='7 days',exact=True).click()
    expect(charts.get_by_role('button',name='7 days',exact=True)).to_have_attribute('aria-pressed','true')
    charts.get_by_label('Time grouping',exact=True).select_option('day')
    expect(charts.locator('.lp-period-table tbody tr')).to_have_count(7)
    charts.get_by_label('Filter genre',exact=True).select_option('Jazz')
    expect(charts.locator('.lp-empty')).to_be_visible()  # artist A + Jazz has no plays
    charts.get_by_role('button',name='Clear filters',exact=True).click()
    expect(charts.locator('.lp-empty')).not_to_be_visible()
    page.go_back()
    expect(charts.locator('.lp-empty')).to_be_visible()
    page.go_forward()
    expect(charts.locator('.lp-empty')).not_to_be_visible()
    with page.expect_download() as exported:
        charts.get_by_role('button',name='Export CSV',exact=True).click()
    assert exported.value.suggested_filename.startswith('listening-')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.locator('body *').evaluate_all(
        'els => els.filter(e => e.getBoundingClientRect().right > innerWidth + 1).slice(0,8).map(e => e.className)')
    charts.screenshot(path=f'/tmp/tuneconsole-listening-charts-{width}.png')
    page.get_by_role('button',name='Top songs',exact=True).click()
    expect(page.locator('.chart-panel:visible table')).to_be_visible()
    expect(page.locator('.chart-window a.on')).to_have_text('7d')
    assert not errors, errors


@pytest.mark.parametrize('width',[1440,390])
def test_new_capture_shows_activity_without_claiming_discovery(live_app,store,page,width):
    store.settings.set_setting('listening_timezone','UTC')
    for index in range(100):
        key, video = f'new-{index}', f'new-video-{index}'
        seed_catalog_track(store,key,f'Song {index}',f'Artist {index}',video_id=video)
        store.history.record_play_event(1,key,video,NOW-(index%5)*86400)
    assert listening_analytics.build(store,NOW)
    page.set_viewport_size({'width':width,'height':1100})
    page.goto(live_app+'/charts?view=analytics&window=30d')
    charts=page.locator('[data-listening-analytics]')
    expect(charts.locator('.lp-summary')).to_be_visible()
    expect(charts.get_by_role('region',name='Artist listening histories',exact=True)).to_be_visible()
    expect(charts.get_by_role('region',name='Discovery rates',include_hidden=True)).to_be_hidden()
    assert charts.evaluate("el => Alpine.$data(el).result.total.plays") == 100
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


@pytest.mark.parametrize('width',[1440,390])
def test_daily_orbit_players_follow_their_arcs_without_overlapping_dates(live_app,store,page,width):
    store.settings.set_setting('listening_timezone','UTC')
    seed_catalog_track(store,'old','Earlier song','Returning artist',video_id='old-video')
    for ago in (110,108):
        store.history.record_play_event(1,'old','old-video',NOW-ago*86400)
    for ago in range(14):
        key,video=f'day-{ago}',f'daily-video-{ago}'
        seed_catalog_track(store,key,f'Song from {ago} days ago','Returning artist',video_id=video,
            thumbnail=f'/test-cover-{ago}.svg')
        store.history.record_play_event(1,key,video,NOW-ago*86400)
    assert listening_analytics.build(store,NOW)
    page.set_viewport_size({'width':width,'height':1100})
    page.route('**/play',lambda route:route.fulfill(json={'ok':True}))
    page.route('**/test-cover-*.svg',lambda route:route.fulfill(content_type='image/svg+xml',
        body='<svg xmlns="http://www.w3.org/2000/svg" width="56" height="56"><rect width="56" height="56" fill="gold"/></svg>'))
    page.goto(live_app+'/charts')
    lead=page.locator('.le-lead')
    expect(lead.locator('h2')).to_have_text('Back to Returning artist.')
    players=lead.locator('.le-song-play')
    expect(players).to_have_count(14)
    expect(players.locator('foreignObject img')).to_have_count(14)
    assert players.locator('img').evaluate_all('''images => images.every(image =>
        image.referrerPolicy==='no-referrer' && getComputedStyle(image).borderRadius==='8px')''')
    assert players.evaluate_all("els => els.map(el => el.getAttribute('href'))")==[
        f'https://music.youtube.com/watch?v=daily-video-{ago}' for ago in reversed(range(14))]
    assert lead.locator('.le-composition > svg').evaluate('''svg => {
        const bounds=svg.viewBox.baseVal, center={x:bounds.width/2,y:bounds.height/2};
        const sleeves=[...svg.querySelectorAll('.le-song-sleeve')].map(el => el.getBBox());
        const dates=[...svg.querySelectorAll('.le-svg-date')].map(el => el.getBBox());
        const arcs=[...svg.querySelectorAll('.le-orbit-arc')];
        const overlaps=(a,b) => a.x<b.x+b.width && b.x<a.x+a.width && a.y<b.y+b.height && b.y<a.y+a.height;
        return sleeves.every((box,i) => {
            const point=arcs[i].getPointAtLength(arcs[i].getTotalLength()/2);
            const x=box.x+box.width/2-center.x, y=box.y+box.height/2-center.y;
            const ax=point.x-center.x, ay=point.y-center.y;
            return box.x>=0 && box.y>=0 && box.x+box.width<=bounds.width && box.y+box.height<=bounds.height
                && Math.abs(x*ay-y*ax)/Math.hypot(x,y)/Math.hypot(ax,ay)<.001
                && !dates.some(date => overlaps(box,date))
                && !sleeves.some((other,j) => i!==j && overlaps(box,other));
        });
    }''')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.evaluate('document.fonts.ready')
    with page.expect_request('**/play') as playback:
        players.first.click()
    assert playback.value.post_data_json=={'url':'https://music.youtube.com/watch?v=daily-video-13'}
    assert len(page.context.pages)==1


def test_editorial_covers_load_without_referrers_and_recover_from_broken_urls(live_app,store,page):
    from base64 import b64encode

    store.settings.set_setting('listening_timezone','UTC')
    for key,thumbnail in [('old','/editorial-cover.svg'),('new','/editorial-cover-missing.svg')]:
        seed_catalog_track(store,key,key.title(),'Artist',video_id='cover-'+key,thumbnail=thumbnail)
    for ago in (70,72):
        store.history.record_play_event(1,'old','cover-old',NOW-ago*86400)
    for key,ago,count in [('old',3,3),('new',2,2),('new',1,2)]:
        for index in range(count):
            store.history.record_play_event(1,key,'cover-'+key,NOW-ago*86400+index*3600)
    assert listening_analytics.build(store,NOW)
    requests=[]
    cover='<svg xmlns="http://www.w3.org/2000/svg" width="56" height="56"><rect width="56" height="56" fill="#ff0000"/></svg>'

    def respond(route):
        requests.append(route.request)
        if route.request.url.endswith('missing.svg') or 'referer' in route.request.headers:
            route.fulfill(status=404)
        else:
            route.fulfill(content_type='image/svg+xml',body=cover)

    page.route('**/editorial-cover*',respond)
    page.route('https://i.ytimg.com/vi/cover-*/hqdefault.jpg',respond)
    page.goto(live_app+'/charts')
    images=page.locator('.le-song-play img')
    expect(images).to_have_count(3)
    expect(images.nth(1)).to_have_attribute('src','https://i.ytimg.com/vi/cover-new/hqdefault.jpg')
    page.wait_for_function("[...document.querySelectorAll('.le-song-play img')].every(i=>i.complete && i.naturalWidth===56)")
    assert requests and all('referer' not in r.headers for r in requests)
    page.evaluate('document.fonts.ready')
    player=page.locator('.le-song-play').first
    page.mouse.move(0,0)
    raster='data:image/png;base64,'+b64encode(player.screenshot(animations='disabled')).decode()
    pixels=page.evaluate('''async source => {
        const image=new Image(); image.src=source; await image.decode();
        const canvas=document.createElement('canvas'); canvas.width=image.width; canvas.height=image.height;
        const context=canvas.getContext('2d'); context.drawImage(image,0,0);
        return {center:[...context.getImageData(Math.floor(image.width/2),Math.floor(image.height/2),1,1).data],
            corner:[...context.getImageData(0,0,1,1).data]};
    }''',raster)
    assert pixels['center'][:3]==[255,0,0]
    assert pixels['corner'][:3]!=[255,0,0]
    expect(player.locator('.le-play-icon')).to_have_css('opacity','0')
    player.hover()
    expect(player.locator('.le-play-icon')).to_have_css('opacity','1')
    assert player.locator('img').evaluate("img=>getComputedStyle(img).borderRadius")=='8px'


def test_imported_sightings_have_their_own_history(live_app,store,page):
    seed_catalog_track(store,'import','Imported song','Imported artist',video_id='import-video',genre='Jazz')
    store.history.record_history_plays(1,NOW-86400,['import'],source_timezone='UTC')
    assert listening_analytics.build(store,NOW)
    page.goto(live_app + '/charts?view=analytics')
    charts = page.locator('[data-listening-analytics]')
    expect(charts.locator('.lp-history-row a')).to_have_text('Imported artist')
    expect(charts.get_by_role('group',name='Listening history units').get_by_role('button',name='Daily history entries',exact=True)).to_have_attribute('aria-pressed','true')
    expect(charts.locator('.lp-total-row')).to_contain_text('All history entries')
    expect(charts.locator('.lp-history-row strong')).to_have_text('1')
    expect(charts.locator('.lp-card:visible')).to_have_count(2)
    charts.get_by_role('button',name='Recorded plays',exact=True).click()
    expect(charts.locator('.lp-history-row')).to_have_count(0)


def test_preparing_retry_and_empty_state(live_app,page):
    state = {'ready':False}
    empty = {'schema':2,'today':'2026-10-24','built_at':NOW,'timezone':'UTC',
        'columns':['day','artist','genre','format',*listening_analytics.FIELDS,'track','artist_id'],'chunks':[]}
    page.route('**/charts/listening-data',lambda route:route.fulfill(
        status=200 if state['ready'] else 202,json=empty if state['ready'] else {'status':'preparing'}))
    page.goto(live_app + '/charts?view=analytics')
    charts = page.locator('[data-listening-analytics]')
    expect(charts.get_by_role('status')).to_contain_text('Preparing listening charts')
    state['ready'] = True
    expect(charts.locator('.lp-empty')).to_be_visible(timeout=10000)
    expect(charts.locator('.lp-summary')).to_contain_text('0 recorded plays')


def test_home_has_no_journal_and_hidden_charts_stories_do_not_count(live_app,store,page):
    prepare(store)
    requests=[]
    page.on('request',lambda req: requests.append(req.url))
    page.goto(live_app + '/#directions')
    expect(page.get_by_role('tab',name='Charts',exact=True)).to_have_count(0)
    expect(page.locator('[data-listening-edition]')).to_have_count(0)
    page.wait_for_timeout(1700)
    assert not any('/charts/listening-' in url for url in requests)
    page.goto(live_app+'/charts?view=songs')
    page.wait_for_timeout(1700)
    assert not any('/charts/listening-' in url for url in requests)
    page.locator('.chart-views').get_by_role('button',name='Home',exact=True).click()
    expect(page.locator('.le-lead')).to_be_visible()
    page.wait_for_timeout(1800)
    assert any('/charts/listening-impressions' in url for url in requests)
    assert not any('/charts/listening-data' in url or '/charts/listening-chunk' in url for url in requests)
    page.locator('.chart-views').get_by_role('button',name='Listening analytics',exact=True).click()
    expect(page.locator('.lp-summary')).to_be_visible()
    page.go_back()
    expect(page.locator('.le-lead')).to_be_visible()


def test_sparse_history_hides_charts_home_and_sightings_do_not_create_filler(live_app,store,page):
    seed_catalog_track(store,'sparse','One song','One artist',video_id='sparse-video')
    store.history.record_history_plays(1,NOW-86400,['sparse'],source_timezone='UTC')
    assert listening_analytics.build(store,NOW)
    store.settings.set_setting('last_sync_at','1')
    store.settings.set_setting('onboard_dismissed','1')
    prepare_home(store,1)
    from tests.onboarding_support import publish_personal_mix
    publish_personal_mix(store,1)
    page.goto(live_app + '/charts')
    expect(page.locator('.chart-views').get_by_role('button',name='Home',exact=True)).to_have_count(0)
    expect(page.locator('[data-listening-edition]')).to_have_count(0)


def test_confirmed_heart_without_playback_does_not_create_empty_charts(live_app,store,page):
    seed_catalog_track(store,'choice','A choice','One artist',video_id='choice-video')
    ratings.record_confirmed(store,1,'choice-video','LIKE',NOW-100)
    assert listening_analytics.build(store,NOW)
    page.goto(live_app+'/charts?view=analytics')
    charts=page.locator('[data-listening-analytics]')
    expect(charts.locator('.lp-card:visible')).to_have_count(2)
    expect(charts.locator('.lp-feedback-chart')).not_to_be_visible()
    expect(charts.locator('.lp-rating-row:visible')).to_have_count(1)
    expect(charts.locator('.lp-rating-row:visible')).to_contain_text('Hearts given')


def test_long_explicit_daily_range_keeps_resolution_with_bounded_tables(live_app,store,page):
    from datetime import datetime,timezone
    seed_catalog_track(store,'long','A long history','One artist',video_id='long-video')
    for at in (datetime(2000,1,1,12,tzinfo=timezone.utc).timestamp(),NOW-100):
        store.history.record_play_event(1,'long','long-video',at)
    assert listening_analytics.build(store,NOW)
    errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(live_app+'/charts?view=analytics&window=all&period=day')
    charts=page.locator('[data-listening-analytics]')
    expect(charts.locator('.lp-resolution')).to_contain_text('By day',timeout=15000)
    expect(charts.locator('.lp-period-table tbody tr')).to_have_count(100)
    expect(charts.locator('.lp-total-bar')).to_have_count(2)
    expect(charts.locator('.lp-feedback-chart rect')).to_have_count(0)
    charts.get_by_role('button',name='Next periods',exact=True).click()
    expect(charts.locator('.lp-period-table tbody tr').first).to_contain_text('Apr 10, 2000')
    assert not errors,errors
