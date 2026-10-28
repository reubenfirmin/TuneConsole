"""Provider boundaries retain native tags/counts and their actual entity scope."""
from yt_playlist.providers import discogs, lastfm, musicbrainz


def test_lastfm_preserves_multiple_track_tags_counts_and_verified_match(monkeypatch):
    monkeypatch.setattr(lastfm, '_get', lambda params: {'track': {
        'name': 'Song', 'mbid': 'recording-id', 'artist': {'name': 'Artist', 'mbid': 'artist-id'},
        'toptags': {'tag': [{'name': 'Metal', 'count': '87'}, {'name': 'Ambient', 'count': '42'},
                            {'name': 'seen live', 'count': '900'}]}}})
    result = lastfm.probe({'title': 'Song', 'artist': 'Artist'}, key='fixture')
    assert len(result.observations) == 1
    tags = result.observations[0].payload()
    assert (tags['scope'], tags['entity_id']) == ('recording', 'recording-id')
    assert tags['value'] == [{'name': 'Metal', 'count': '87'}, {'name': 'Ambient', 'count': '42'},
                              {'name': 'seen live', 'count': '900'}]
    assert tags['match_evidence']['title_artist_match'] is True


def test_lastfm_artist_fallback_retains_artist_scope_and_stable_id(monkeypatch):
    def get(params):
        if params['method'] == 'track.getinfo':
            return {'track': {'name': 'Song', 'mbid': 'recording-id',
                'artist': {'name': 'Artist', 'mbid': 'artist-id'}, 'toptags': {'tag': []}}}
        return {'toptags': {'tag': [{'name': 'Metal', 'count': '80'}, {'name': 'Ambient', 'count': '50'}]}}
    monkeypatch.setattr(lastfm, '_get', get)
    result = lastfm.probe({'title': 'Song', 'artist': 'Artist'}, key='fixture')
    assert [(row.scope, row.entity_id) for row in result.observations] == [
        ('recording', 'recording-id'), ('artist', 'artist-id')]
    assert len(result.observations[1].value) == 2


def test_musicbrainz_reuses_native_artist_cache_without_flattening_counts(monkeypatch):
    import io
    import json
    from types import SimpleNamespace
    from yt_playlist.integrations.musicbrainz import MusicBrainzIAO
    calls = []
    def get(path, params):
        calls.append(path)
        if path == 'recording':
            return {'recordings': [{'id': 'recording-id', 'title': 'Song', 'score': 100,
                'artist-credit': [{'artist': {'id': 'artist-id', 'name': 'Artist'}}]}]}
        if path.startswith('recording/'):
            return {'id': 'recording-id', 'title': 'Song', 'tags': []}
        return {'id': 'artist-id', 'name': 'Artist',
                'genres': [{'name': 'Metal', 'count': 7}], 'tags': [{'name': 'Ambient', 'count': 3}]}
    def open_request(request, **kwargs):
        from urllib.parse import urlsplit
        path = urlsplit(request.full_url).path.removeprefix('/ws/2/')
        return io.StringIO(json.dumps(get(path, {})))
    monkeypatch.setattr(musicbrainz, '_iao', MusicBrainzIAO(open_fn=open_request,
                        pacer=SimpleNamespace(wait=lambda: None)))
    for _ in range(2):
        result = musicbrainz.probe({'title': 'Song', 'artist': 'Artist'})
        artist = next(row for row in result.observations if row.scope == 'artist')
        assert artist.entity_id == 'artist-id'
        assert artist.value == [{'name': 'Metal', 'count': 7, 'kind': 'genres'},
                                {'name': 'Ambient', 'count': 3, 'kind': 'tags'}]
    assert calls.count('artist/artist-id') == 1


def test_discogs_multiple_styles_stay_release_scoped_and_unrelated_hits_are_excluded(monkeypatch):
    monkeypatch.setattr(discogs, '_search', lambda query, tok: [
        {'id': 1, 'title': 'Artist - Song', 'style': ['Doom Metal', 'Ambient'], 'genre': ['Rock']},
        {'id': 2, 'title': 'Someone else - Song', 'style': ['House']}])
    result = discogs.probe({'title': 'Song', 'artist': 'Artist'}, tok='fixture')
    assert len(result.observations) == 1
    tags = result.observations[0]
    assert (tags.scope, tags.entity_id) == ('release', '1')
    assert tags.value == [{'name': 'Doom Metal', 'kind': 'style'}, {'name': 'Ambient', 'kind': 'style'},
                          {'name': 'Rock', 'kind': 'genre'}]
