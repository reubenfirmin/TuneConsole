"""Home and Charts use the prepared listening data and TuneConsole's shared styles."""
import json
import math

from playwright.sync_api import expect
import pytest

from tests.conftest import seed_catalog_track
from tests.test_mode_cards_route import prepare_home
from yt_playlist.rec import listening_analytics
from yt_playlist.library import ratings

NOW = 1792843200.0  # 2026-10-24 UTC


def prepare(store):
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
def test_home_and_charts_filtering_and_shared_styles(live_app,store,page,width):
    prepare(store)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/bridge/status',lambda route:route.fulfill(json={'connected':True}))
    page.set_viewport_size({'width':width,'height':1100})
    page.goto(live_app + '/#portrait')
    panel = page.get_by_role('tabpanel',name='Your listening',exact=True)
    expect(panel.locator('.lp-summary')).to_be_visible()
    expect(panel.locator('.lp-card')).to_have_count(4)
    expect(panel.locator('.lp-history-row')).to_have_count(18)
    expect(panel.locator('.lp-total-plot svg rect')).to_have_count(90)
    assert panel.locator('.lp-discovery-dot').count() > 0
    assert panel.locator('.lp-feedback-liked').count() == 90
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.locator('body *').evaluate_all(
        'els => els.filter(e => e.getBoundingClientRect().right > innerWidth + 1).slice(0,8).map(e => e.className)')
    typography = panel.locator('h2').evaluate('(el) => getComputedStyle(el).fontFamily')
    assert 'Hanken Grotesk' in typography and 'Fraunces' not in typography
    assert panel.locator('.lp-card').first.evaluate('(el) => getComputedStyle(el).borderRadius') == '0px'
    assert panel.locator('[class*="eyebrow"], .lp-index').count() == 0
    panel.locator('.lp-history-plot').first.focus()
    expect(panel.locator('.lp-readout')).to_contain_text('plays')
    panel.screenshot(path=f'/tmp/tuneconsole-listening-home-{width}.png')
    panel.get_by_role('link',name='More in Charts').click()
    charts = page.locator('[data-listening-analytics]')
    expect(charts.locator('.lp-summary')).to_be_visible()
    expect(charts.locator('.lp-card')).to_have_count(5)
    charts.get_by_label('Filter artist',exact=True).select_option('Artist A')
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
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.locator('body *').evaluate_all(
        'els => els.filter(e => e.getBoundingClientRect().right > innerWidth + 1).slice(0,8).map(e => e.className)')
    charts.screenshot(path=f'/tmp/tuneconsole-listening-charts-{width}.png')
    page.get_by_role('button',name='Top songs',exact=True).click()
    expect(page.locator('.chart-panel:visible table')).to_be_visible()
    assert not errors, errors


def test_imported_sightings_have_their_own_history(live_app,store,page):
    seed_catalog_track(store,'import','Imported song','Imported artist',video_id='import-video',genre='Jazz')
    store.history.record_history_plays(1,NOW-86400,['import'],source_timezone='UTC')
    assert listening_analytics.build(store,NOW)
    page.goto(live_app + '/charts?view=analytics')
    charts = page.locator('[data-listening-analytics]')
    expect(charts.locator('.lp-history-row a')).to_have_text('Imported artist')
    expect(charts.get_by_role('button',name='Dated sightings',exact=True)).to_have_attribute('aria-pressed','true')
    expect(charts.locator('.lp-total-row')).to_contain_text('All sightings')
    expect(charts.locator('.lp-history-row strong')).to_have_text('1')
    charts.get_by_role('button',name='Recorded plays',exact=True).click()
    expect(charts.locator('.lp-history-row')).to_have_count(0)


def test_preparing_retry_and_empty_state(live_app,page):
    state = {'ready':False}
    empty = {'schema':1,'today':'2026-10-24','built_at':NOW,
        'columns':['day','artist','genre','format',*listening_analytics.FIELDS],'rows':[]}
    page.route('**/charts/listening-data',lambda route:route.fulfill(
        status=200 if state['ready'] else 202,json=empty if state['ready'] else {'status':'preparing'}))
    page.goto(live_app + '/charts?view=analytics')
    charts = page.locator('[data-listening-analytics]')
    expect(charts.get_by_role('status')).to_contain_text('Preparing listening charts')
    state['ready'] = True
    expect(charts.locator('.lp-empty')).to_be_visible(timeout=10000)
    expect(charts.locator('.lp-summary')).to_contain_text('0 recorded plays')
