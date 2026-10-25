"""Track actions and navigation work on desktop and narrow screens."""
import pytest
from playwright.sync_api import expect

from tests.browser.test_song_actions_browser import song_app  # noqa: F401 - shared fixture
from tests.test_song_actions import SONG
from yt_playlist.web.track_links import track_url


@pytest.mark.parametrize('width', [1280, 390])
def test_musical_evidence_opens_with_keyboard_and_fits_narrow_screens(song_app, page, width):
    from tests.musical_support import prepare_profiles
    from yt_playlist.providers.base import MetadataObservation
    store = song_app['store']
    tid = store.tracks.track_ids_for_videos(['owned-song'])['owned-song']
    store.enrichment_state.record_metadata_observations(tid, 'acousticbrainz', [MetadataObservation(
        'mood_relaxed', .8, 'recording', 'recording-one', 'prediction', unit='probability',
        model_version='relaxed-model-v1', match_evidence={'method': 'supplied_recording_mbid'})],
        run_id='one', now=10)
    prepare_profiles(store)
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(song_app['base'] + '/track/owned-song')
    evidence = page.locator('.track-musical-evidence')
    expect(evidence).not_to_have_attribute('open', '')
    expect(evidence.locator('.track-musical-signals')).not_to_be_visible()
    evidence.locator('> summary').focus()
    page.keyboard.press('Enter')
    expect(evidence).to_have_attribute('open', '')
    expect(evidence).to_contain_text('Used by On Air and Mix Tapes')
    expect(evidence.locator('[data-musical-signal="relaxed"]')).to_contain_text('Model prediction')
    expect(evidence.locator('[data-musical-signal="tempo"]')).to_contain_text('Unknown')
    evidence.locator('.track-musical-sources > summary').click()
    expect(evidence).to_contain_text('relaxed-model-v1')
    expect(evidence).to_contain_text('mood relaxed: 0.8 (probability)')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=f'/tmp/tuneconsole-405-provenance-{width}.png', full_page=True)


@pytest.mark.parametrize('width', [1280, 390])
def test_track_details_actions_and_navigation(song_app, page, width, tmp_path):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.get_by_role('link', name='Owned Song', exact=True).click()
    expect(page.get_by_role('heading', name='Owned Song', exact=True)).to_be_visible()
    expect(page.get_by_role('link', name='Source mix', exact=True)).to_be_visible()
    art = page.locator('.track-detail-art').bounding_box()
    hero = page.locator('.library-hero.release-hero').bounding_box()
    assert art['width'] >= hero['width'] and art['height'] >= hero['height']
    ledger = page.locator('[aria-labelledby="track-ledger-title"]')
    expect(ledger.get_by_role('heading', name='Listening history')).to_be_visible()
    like = page.get_by_role('button', name='Like', exact=True)
    dislike = page.get_by_role('button', name='Dislike', exact=True)
    expect(like).to_have_text('')
    expect(dislike).to_have_text('')
    expect(page.get_by_role('link', name='Play on YouTube Music')).to_have_text('')
    like.click()
    expect(like).to_have_attribute('aria-pressed', 'true')
    dislike.click()
    expect(dislike).to_have_attribute('aria-pressed', 'true')
    expect(like).to_have_attribute('aria-pressed', 'false')
    assert song_app['main'].rated[-2:] == [('owned-song', 'LIKE'), ('owned-song', 'DISLIKE')]
    page.get_by_role('button', name='Create playlist with song', exact=True).click()
    dialog = page.get_by_role('dialog', name='Create playlist', exact=True)
    expect(dialog.get_by_label('Playlist name')).to_be_focused()
    dialog.get_by_label('Playlist name').fill('Track favorites')
    dialog.get_by_role('button', name='Create playlist', exact=True).click()
    expect(dialog.get_by_text('Playlist created.', exact=True)).to_be_visible()
    dialog.get_by_role('button', name='Done', exact=True).click()
    expect(page.get_by_role('link', name='Track favorites', exact=True)).to_be_visible()
    page.get_by_role('button', name='Edit metadata', exact=True).click()
    edit = page.get_by_role('dialog', name='Edit Metadata', exact=True)
    edit.get_by_label('Title', exact=True).fill('Corrected song')
    edit.get_by_role('button', name='Save changes', exact=True).click()
    expect(page.get_by_role('heading', name='Corrected song', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=tmp_path / f'track-{width}.png', full_page=True, animations='disabled')
    assert not errors


@pytest.mark.parametrize('width', [1440, 390])
def test_populated_track_keeps_memberships_and_precise_ledger_together(song_app, page, width):
    store = song_app['store']
    track = store.tracks.track_detail('owned-song')
    store.history.record_play_event(song_app['main_id'], track['identity_key'], 'owned-song',
                                    1792785600, source_timezone='UTC')
    store.history.record_history_plays(song_app['main_id'], 1792785660,
                                      [track['identity_key']], source_timezone='UTC')
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(song_app['base'] + '/track/owned-song')
    expect(page.get_by_role('link', name='Source mix', exact=True)).to_be_visible()
    ledger = page.locator('[aria-labelledby="track-ledger-title"]')
    expect(ledger).to_contain_text('History observation')
    expect(ledger).to_contain_text('Played')
    expect(ledger).to_contain_text('History observations do not establish an exact play time or count.')
    memberships = page.locator('[aria-labelledby="track-playlists-title"]').bounding_box()
    assert ledger.bounding_box()['y'] > memberships['y'] + memberships['height']
    assert abs(ledger.bounding_box()['width'] - memberships['width']) < 2
    for identity in store.identities.get_identities():
        for section in ['.track-memberships', '.track-ledger']:
            expect(page.locator(section)).not_to_contain_text(identity.label)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=f'/tmp/tuneconsole-386-populated-{width}.png', full_page=True)


@pytest.mark.parametrize('reload_page', [False, True], ids=['already-open', 'after-reload'])
def test_native_verified_play_populates_track_history(song_app, page, reload_page):
    from types import SimpleNamespace
    from yt_playlist.library.live_plays import handle_play_event
    store = song_app['store']
    store.identities.upsert_identity('Native player', 'native-player-cred', 'native-player-brand', False)
    video = 'FDuJmIOtT7I'
    title = "Most Precious Love (DF's Future 3000 Extended Mix) (feat. Barbara Tucker)"
    store.tracks.upsert_track(video, title, 'Blaze', '', 480,
                             thumbnail='/static/onboarding-albums.png')
    page.goto(song_app['base'] + '/track/' + video)
    expect(page.get_by_text('No listening evidence recorded.', exact=True)).to_be_visible()
    assert handle_play_event(SimpleNamespace(store=store), {
        'type': 'play', 'videoId': video, 'title': 'Unverified player title',
        'artist': 'Uploader channel', 'metadataVerified': False, 'videoVerified': True,
        'brandKnown': True, 'brandId': 'native-player-brand', 'paused': False, 'volume': 50,
    }, 1792890000)
    if reload_page:
        page.reload()
    expect(page.locator('.track-ledger strong')).to_have_text(['Played'], timeout=5000)
    expect(page.get_by_role('heading', name=title, exact=True)).to_be_visible()


def test_discovery_album_save_and_rating_error(song_app, page, monkeypatch):
    page.goto(song_app['base'] + track_url(SONG))
    expect(page.get_by_role('heading', name=SONG['title'], exact=True)).to_be_visible()
    page.route('**/now-playing/rate', lambda route: route.fulfill(
        status=200, json={'ok': False, 'error': 'Rating unavailable'}))
    page.get_by_role('button', name='Like', exact=True).click()
    expect(page.get_by_role('alert').filter(has_text='Rating unavailable')).to_be_visible()
    expect(page.get_by_role('button', name='Like', exact=True)).to_have_attribute('aria-pressed', 'false')
    song_app['main']._albums['MPRE_new']['audioPlaylistId'] = 'ALBUM_PLAYLIST'
    monkeypatch.setattr(song_app['main'], 'rate_playlist', lambda *args: None, raising=False)
    page.get_by_role('button', name='Save album to collection', exact=True).click()
    expect(page.get_by_text('Saved to collection', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Save album to collection', exact=True)).to_have_count(0)
