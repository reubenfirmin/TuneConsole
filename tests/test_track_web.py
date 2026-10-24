"""Track details retain exact-upload collection facts and honest listening precision."""
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_song_actions import SONG, songs  # noqa: F401 - shared fixture
from yt_playlist.web.track_links import track_url


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
    assert 'Play ledger' in body and 'No listening evidence recorded' in body


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
