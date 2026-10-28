"""Genre evidence belongs to the requested mix rather than the original artist or recording."""
import pytest

from yt_playlist.providers import discogs, lastfm, musicbrainz


@pytest.mark.parametrize('title', [
    "Satellite Serenade (The Orb's Transasianexpress Mix) [Mixed]",
    'Song (Club Remix)', 'Song - A Different Producer Rework',
])
@pytest.mark.parametrize('recording_genre', [None, 'Ambient'])
def test_musicbrainz_remix_uses_only_its_recording_genre(monkeypatch, title, recording_genre):
    calls = []

    def get(path, params):
        calls.append(path)
        if path == 'recording':
            return {'recordings': [{'id': 'mix', 'title': title, 'first-release-date': '1993',
                'artist-credit': [{'name': 'Artist', 'artist': {'id': 'original-artist'}}]}]}
        if path == 'recording/mix':
            return {'genres': [{'name': recording_genre, 'count': 1}]} if recording_genre else {}
        raise AssertionError('A remix must not inherit the original artist genre')

    monkeypatch.setattr(musicbrainz, '_get', get)
    assert musicbrainz.enrich_full(title, 'Artist')[:3] == (recording_genre, '1993', 'mix')
    assert calls == ['recording', 'recording/mix']


@pytest.mark.parametrize('recording_genre', [None, 'Ambient'])
def test_lastfm_remix_does_not_fall_back_to_artist_tags(monkeypatch, recording_genre):
    def get(params):
        assert params['method'] == 'track.getinfo'
        return {'track': {'toptags': {'tag': [{'name': recording_genre}]}}} if recording_genre else {}

    monkeypatch.setattr(lastfm, '_get', get)
    assert lastfm.enrich('Song (Club Mix)', 'Artist', 'key') == (recording_genre, None)


@pytest.mark.parametrize('title', ['Song', 'Mixed Emotions'])
def test_unremixed_song_can_still_use_artist_genre(monkeypatch, title):
    calls = []

    def get(params):
        calls.append(params['method'])
        return {'toptags': {'tag': [{'name': 'Jazz'}]}} if params['method'] == 'artist.gettoptags' else {}

    monkeypatch.setattr(lastfm, '_get', get)
    assert lastfm.enrich(title, 'Artist', 'key') == ('Jazz', None)
    assert calls == ['track.getinfo', 'artist.gettoptags']


def test_discogs_original_recording_cannot_supply_remix_genre_or_year(monkeypatch):
    monkeypatch.setattr(discogs, '_search', lambda *a: [
        {'id': 1, 'title': 'Artist - Song', 'genre': ['Rock'], 'year': 1970},
        {'id': 2, 'title': 'Artist - Song (Club Remix)', 'style': ['Deep House'], 'year': 2024},
    ])
    monkeypatch.setattr(discogs, '_get', lambda *a, **kw: {'tracklist': [{'title': 'Song'}],
                                                  'artists': [{'name': 'Artist'}]})
    assert discogs.enrich('Song (Club Remix)', 'Artist') == ('Deep House', '2024')

