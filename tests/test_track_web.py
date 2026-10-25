"""Track details retain exact-upload collection facts and honest listening precision."""
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_song_actions import SONG, songs  # noqa: F401 - shared fixture
from yt_playlist.web.track_links import track_url


def test_musical_evidence_uses_saved_profiles_without_foreground_preparation(songs, monkeypatch):
    from tests.musical_support import prepare_profiles
    from yt_playlist.providers.base import MetadataObservation
    from yt_playlist.rec import musical_profiles
    store = songs['store']
    tid = store.tracks.track_ids_for_videos(['owned-song'])['owned-song']
    store.enrichment_state.record_metadata_observations(tid, 'acousticbrainz', [MetadataObservation(
        'mood_aggressive', .9, 'recording', 'recording-one', 'prediction', unit='probability',
        model_version={'name': 'mood-aggressive', 'version': '1'}, provider_version='native-v1',
        match_evidence={'method': 'supplied_recording_mbid'})], run_id='one', now=10)
    prepare_profiles(store)
    key = store.tracks.identity_key_for_video('owned-song')
    def unexpected(*args, **kwargs):
        pytest.fail('The track page must only read prepared musical evidence')
    monkeypatch.setattr(musical_profiles, 'source_profile', unexpected)
    monkeypatch.setattr(musical_profiles, 'resolve', unexpected)
    monkeypatch.setattr(songs['main'], 'get_song', unexpected)
    before = store.conn.total_changes
    body = songs['http'].get('/track/owned-song').text
    evidence = body.split('class="track-musical-evidence"')[1].split('</section>')[0]
    assert 'Used by On Air and Mix Tapes' in evidence
    assert 'Model prediction' in evidence and 'AcousticBrainz (recording)' in evidence
    assert 'mood aggressive: 0.9 (probability)' in evidence
    assert 'native-v1' in evidence and 'mood-aggressive' in evidence
    assert 'Recording ID lookup' in evidence and 'not been manually verified' in evidence
    assert 'Unknown' in evidence and 'confidence that the recording matches' in evidence
    assert store.conn.total_changes == before
    profile = store.musical_profiles.by_keys([key])[key]
    # A source cache can advance before the recording resolver gets its next turn.
    # Its new observations must not be attributed to the older recording result.
    with store.conn:
        store.conn.execute("UPDATE rec_musical_source_profiles SET digest='newer-source'")
    body = songs['http'].get('/track/owned-song').text
    assert 'Some source details are unavailable or awaiting refresh.' in body
    assert 'mood aggressive: 0.9 (probability)' not in body
    assert store.musical_profiles.by_keys([key])[key] == profile


def test_musical_evidence_distinguishes_legacy_hints_corrections_and_unknowns(songs):
    from tests.musical_support import prepare_profiles
    from yt_playlist.core import catalog
    store = songs['store']
    tid = store.tracks.track_ids_for_videos(['owned-song'])['owned-song']
    with store.conn:
        store.conn.execute("UPDATE recordings SET genre='Ambient',danceability=.8 WHERE id=("
                           'SELECT recording_id FROM catalog_entries WHERE id=?)', (tid,))
        catalog.choose_field(store.conn, tid, 'energy', .9)
        catalog.choose_field(store.conn, tid, 'mood_happy', None)
    prepare_profiles(store)
    body = songs['http'].get('/track/owned-song').text
    intensity = body.split('data-musical-signal="intensity"')[1].split('</dd>')[0]
    groove = body.split('data-musical-signal="groove"')[1].split('</dd>')[0]
    relaxed = body.split('data-musical-signal="relaxed"')[1].split('</dd>')[0]
    happiness = body.split('data-musical-signal="happiness"')[1].split('</dd>')[0]
    assert 'High' in intensity and 'Your correction' in intensity and 'Genre hint' not in intensity
    assert 'Legacy metadata' in groove and 'Limited evidence' in groove
    assert 'Genre hint' in relaxed and 'Limited evidence' in relaxed
    assert 'Unavailable after corrections' in happiness and 'Your correction' in happiness


def test_musical_source_details_are_bounded(songs):
    from tests.musical_support import prepare_profiles
    from yt_playlist.providers.base import MetadataObservation
    store = songs['store']
    tid = store.tracks.track_ids_for_videos(['owned-song'])['owned-song']
    for i in range(3):
        store.enrichment_state.record_metadata_observations(tid, f'provider-{i}', [MetadataObservation(
            'mood_relaxed', .9, 'recording', 'same-recording', 'prediction')], run_id=str(i), now=10)
    prepare_profiles(store)
    key = store.tracks.identity_key_for_video('owned-song')
    digests = store.musical_profiles.by_keys([key])[key]['source_versions']
    sources, truncated = store.musical_profiles.provenance_sources(key, digests, limit=1)
    assert len(sources) == 1 and truncated
    sources, truncated = store.musical_profiles.provenance_sources(key, digests, max_bytes=1)
    assert sources == [] and truncated
    assert store.musical_profiles.provenance_sources(key, []) == ([], False)


def test_track_metadata_and_playlist_membership_use_exact_upload(songs):
    store = songs['store']
    owned = store.tracks.track_ids_for_videos(['owned-song'])['owned-song']
    store.playlists.set_playlist_tracks(songs['offline'], [owned])
    other = store.tracks.upsert_track('other-upload', 'Owned Song', 'Artist & Friends', 'Album', 180)
    store.playlists.set_playlist_tracks(songs['destination'], [other])
    store.tracks.update_metadata('owned-song', {'title': 'Corrected title', 'genre': 'Dream Pop', 'year': '2023'},
                                observation={})
    body = songs['http'].get('/track/owned-song?title=Stale&artist=Wrong').text
    assert '<h1>Corrected title</h1>' in body
    assert 'Dream Pop' in body and '<dd>2023</dd>' in body and '<dd>3:00</dd>' in body
    memberships = body.split('id="track-playlists-title"')[1].split('</section>')[0]
    assert 'Source mix' in memberships and 'Offline mix' in memberships
    assert 'Evening mix' not in memberships
    assert 'Artist &amp; Friends' in body
    assert 'Listening history' in body and 'No listening evidence recorded' in body


def test_discovery_preview_retains_metadata_without_saving(songs, monkeypatch):
    store = songs['store']
    before = store.conn.execute('SELECT COUNT(*) FROM catalog_entries').fetchone()[0]
    def unexpected(*args):
        pytest.fail('A preview with metadata must not fetch the song again')
    monkeypatch.setattr(songs['main'], 'get_song', unexpected)
    url = track_url(SONG)
    assert parse_qs(urlsplit(url).query)['title'] == [SONG['title']]
    response = songs['http'].get(url)
    assert response.status_code == 200
    body = response.text
    assert '<h1>A song&#39;s &lt;title&gt;</h1>' in body
    assert 'MPRE_new' in body and 'Save album to collection' in body
    assert '3:45' in body and 'Create playlist with song' in body
    assert store.tracks.track_detail(SONG['video_id']) is None
    assert store.conn.execute('SELECT COUNT(*) FROM catalog_entries').fetchone()[0] == before
    assert not songs['main'].rated and not songs['main'].created


def test_bare_link_resolves_live_and_survives_unavailable_metadata(songs, monkeypatch):
    monkeypatch.setattr(songs['main'], 'get_song', lambda _: {'videoDetails': {
        'title': 'Live preview', 'author': 'An artist', 'lengthSeconds': '61'}})
    assert '<h1>Live preview</h1>' in songs['http'].get('/track/bare-id').text
    def unavailable(*args):
        raise ValueError('Offline')
    monkeypatch.setattr(songs['main'], 'get_song', unavailable)
    response = songs['http'].get('/track/bare-id')
    assert response.status_code == 200 and '<h1>Unknown track</h1>' in response.text
    assert songs['store'].tracks.track_detail('bare-id') is None


@pytest.mark.parametrize('thumbnail,cover', [
    ('https://lh3.googleusercontent.com/album=w120-h120-l90-rj',
     'https://lh3.googleusercontent.com/album=w800-h800-l90-rj'),
    ('https://i.ytimg.com/vi/preview/hqdefault.jpg', 'https://i.ytimg.com/vi/preview/hqdefault.jpg'),
    ('/static/cover.png', '/static/cover.png'),
])
def test_detail_art_requests_display_size_and_retains_original_fallback(songs, thumbnail, cover):
    body = songs['http'].get(track_url({**SONG, 'thumbnail': thumbnail})).text
    assert f'class="track-detail-art" src="{cover}" data-fallback="{thumbnail}"' in body


def test_ledger_shares_confirmed_recording_without_inventing_exact_plays(songs):
    store = songs['store']
    track = store.tracks.track_detail('owned-song')
    store.tracks.upsert_track('same-recording', 'Another title', 'Other spelling', '', 180)
    store.tracks.associate_youtube_item('same-recording', track['recording_id'], source='test',
                                      evidence={'verified': True}, now=1, same_recording=True)
    key = store.tracks.identity_key_for_video('same-recording')
    at = 1792785600  # 2026-10-23 20:00 UTC
    store.history.record_play_event(songs['main_id'], key, 'same-recording', at, source_timezone='UTC')
    store.history.record_history_plays(songs['main_id'], at + 60, [key], source_timezone='UTC')
    store.history.record_history_plays(songs['main_id'], at + 120, [key], source_timezone='UTC')
    other_key = store.tracks.observe_track('unrelated', 'Owned Song (live)', 'Artist & Friends')
    store.history.record_play_event(songs['main_id'], other_key, 'unrelated', at + 5000, source_timezone='UTC')
    rows, more = store.tracks.track_play_ledger(track['recording_id'])
    assert not more and len(rows) == 2
    assert {row['kind'] for row in rows} == {'play', 'observation'}
    assert next(row for row in rows if row['kind'] == 'observation')['precision'] == 'calendar_day'
    body = songs['http'].get('/track/owned-song').text
    assert 'Another upload of this recording' in body
    assert 'History observation' in body and 'Last observed' in body
    assert body.count('<strong>Played</strong>') == 1


def test_ledger_keeps_unknown_history_precision_and_paginates(songs):
    store = songs['store']
    track = store.tracks.track_detail('owned-song')
    for n in range(51):
        store.history.record_play_event(songs['main_id'], track['identity_key'], 'owned-song',
                                        1700000000 + n * 10000, source_timezone='UTC')
    first = songs['http'].get('/track/owned-song').text
    assert first.count('<strong>Played</strong>') == 50 and 'Older entries' in first
    second = songs['http'].get('/track/owned-song?page=1').text
    assert second.count('<strong>Played</strong>') == 1 and 'Newer entries' in second
    assert 'Older entries' not in second
    with store.conn:
        capture = store.conn.execute('INSERT INTO listening_capture_receipts(identity_id,captured_at) '
                                     'VALUES (?,?)', (songs['main_id'], 1800000000)).lastrowid
        store.conn.execute('INSERT INTO listening_observations(identity_id,identity_key,recording_id,'
            'source_day,day,source,precision,first_observed_at,last_observed_at,capture_id,capture_item_id) '
            "VALUES (?,?,?,'2026-10-23','2026-10-23','youtube_history','legacy_unknown',?,?,?,?)",
            (songs['main_id'], track['identity_key'], track['recording_id'], 1800000000, 1800000000, capture, 1))
    body = songs['http'].get('/track/owned-song').text
    assert 'Play time unknown' in body
    assert 'History observations do not establish an exact play time or count.' in body


@pytest.mark.parametrize('page', ['-1', 'bad', '100001'])
def test_ledger_rejects_invalid_pagination(songs, page):
    assert songs['http'].get('/track/owned-song', params={'page': page}).status_code == 422
    assert songs['http'].get('/track/owned-song/history', params={'page': page}).status_code == 422


def test_history_refresh_is_read_only_local_and_keeps_full_page_pagination(songs, monkeypatch):
    store = songs['store']
    def unexpected(*args):
        pytest.fail('History refresh must never fetch metadata or persist a play')
    monkeypatch.setattr(songs['main'], 'get_song', unexpected)
    before = store.conn.total_changes
    assert 'No listening evidence recorded' in songs['http'].get('/track/unknown/history').text
    assert store.conn.total_changes == before
    track = store.tracks.track_detail('owned-song')
    for n in range(51):
        store.history.record_play_event(songs['main_id'], track['identity_key'], 'owned-song',
                                        1700000000 + n * 10000, source_timezone='UTC')
    before = store.conn.total_changes
    response = songs['http'].get('/track/owned-song/history')
    assert response.status_code == 200
    assert response.text.count('<strong>Played</strong>') == 50
    assert 'href="/track/owned-song?page=1"' in response.text
    assert 'hx-get="/track/owned-song/history?page=0"' in response.text
    assert '<html' not in response.text and store.conn.total_changes == before


def test_saved_album_and_song_titles_link_to_details(songs):
    store = songs['store']
    store.tracks.upsert_track('album-song', 'Album song', 'Artist & Friends', 'New Album', 100,
                             album_browse_id='MPRE_new')
    body = songs['http'].get('/album?browse=MPRE_new').text
    assert 'href="/track/new-song?' in body
    assert 'album_browse=MPRE_new' in body
    playlist = songs['http'].get(f"/playlist/{songs['source']}").text
    assert 'class="ptitle" href="/track/owned-song?' in playlist
    assert 'class="pl-play" href="https://music.youtube.com/watch?v=owned-song' in playlist
    search = songs['http'].get('/search/omni?q=Owned').text
    assert 'href="/track/owned-song"' in search


def test_suggestion_objects_link_to_track_details(songs):
    store = songs['store']
    owned = store.playlists.get_playlist_track_ids(songs['source'])[0]
    bonus = store.tracks.upsert_track('bonus', 'Bonus Song', 'Artist & Friends', '', 200)
    for tid in (owned, bonus):
        store.tracks.set_track_genre(tid, 'Techno')
    store.playlists.set_playlist_tracks(songs['destination'], [owned, bonus])
    response = songs['http'].get(f"/playlist/{songs['source']}/suggestions")
    assert response.status_code == 200
    assert 'class="tile-cap-title" href="/track/bonus?' in response.text
