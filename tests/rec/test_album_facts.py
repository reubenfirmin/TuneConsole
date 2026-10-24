import pytest

from yt_playlist.providers import album_facts, deezer, lastfm

ALBUM = {'artist': 'Artist', 'title': 'Record', 'year': '2020'}


def deezer_release():
    return {'id': 42, 'title': 'Record', 'artist': {'name': 'Artist'},
            'release_date': '2020-02-14', 'label': 'Example Records', 'nb_tracks': 2,
            'duration': 180, 'genres': {'data': [{'name': 'Ambient'}]},
            'tracks': {'data': [{'title': 'First'}, {'title': 'Last'}]}}


def test_deezer_album_copy_is_mechanical_attributed_and_uses_bounded_requests(monkeypatch):
    calls = []
    def get(url, *, timeout):
        calls.append((url, timeout))
        return {'data': [deezer_release()]} if '/search/' in url else deezer_release()
    monkeypatch.setattr(deezer, 'tripped', lambda: False)
    monkeypatch.setattr(deezer, '_get_json', get)
    description, evidence = album_facts.from_deezer(ALBUM)
    assert description['text'] == ('Released on 14 February 2020 by Example Records. '
        'The 2-track release runs 3 minutes. Catalogued as Ambient. Opens with First and closes with Last.')
    assert description['acquisition'] == 'automatic' and not description['quote']
    assert description['url'] == 'https://www.deezer.com/album/42'
    assert evidence['response'] == {key: deezer_release().get(key) for key in
        ('id', 'title', 'artist', 'contributors', 'release_date', 'label', 'nb_tracks', 'duration', 'genres', 'tracks')}
    assert len(calls) == 2 and all(timeout == 5 for _, timeout in calls)


@pytest.mark.parametrize('changed', [
    {'title': 'Record (Live)'}, {'artist': {'name': 'Other Artist'}},
    {'release_date': '2021-02-14'}, {'id': 99},
])
def test_deezer_rechecks_artist_title_edition_and_id_after_search(monkeypatch, changed):
    monkeypatch.setattr(deezer, 'tripped', lambda: False)
    monkeypatch.setattr(deezer, '_get_json', lambda url, **kwargs:
                        {'data': [deezer_release()]} if '/search/' in url else {**deezer_release(), **changed})
    description, _ = album_facts.from_deezer(ALBUM)
    assert description is None


def test_deezer_ambiguous_albums_do_not_choose_arbitrary_edition(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return {'data': [deezer_release(), {**deezer_release(), 'id': 43}]}
    monkeypatch.setattr(deezer, 'tripped', lambda: False)
    monkeypatch.setattr(deezer, '_get_json', get)
    assert album_facts.from_deezer(ALBUM)[0] is None
    assert len(calls) == 1


def test_lastfm_reads_album_tags_and_tracklist_without_wiki_or_credentials_in_evidence(store, monkeypatch):
    monkeypatch.setattr(lastfm, 'api_key', lambda store: 'private-key')
    monkeypatch.setattr(lastfm, 'tripped', lambda: False)
    payload = {'album': {'name': 'Record', 'artist': 'Artist',
        'tags': {'tag': [{'name': 'ambient'}]},
        'tracks': {'track': [{'name': 'First'}, {'name': 'Last'}]},
        'wiki': {'summary': 'Unwanted Wikipedia summary'}, 'userplaycount': 999}}
    calls = []
    def get(params, *, timeout):
        calls.append((params, timeout))
        return payload
    monkeypatch.setattr(lastfm, '_get', get)
    description, evidence = album_facts.from_lastfm(ALBUM, store)
    assert description['text'] == ('Listeners tag this release as ambient. '
                                  'Its 2-track listing opens with First and closes with Last.')
    assert description['url'] == 'https://www.last.fm/music/Artist/Record'
    assert 'wiki' not in evidence['response'] and 'userplaycount' not in evidence['response']
    assert 'private-key' not in str(evidence)
    assert calls[0][0]['autocorrect'] == '0' and calls[0][1] == 5
    payload['album']['artist'] = 'Another Artist'
    assert album_facts.from_lastfm(ALBUM, store)[0] is None


def test_lastfm_without_key_is_a_recorded_miss_not_a_network_request(store, monkeypatch):
    monkeypatch.setattr(lastfm, 'api_key', lambda store: None)
    monkeypatch.setattr(lastfm, '_get', lambda *args, **kwargs: pytest.fail('Unconfigured provider must not run'))
    assert album_facts.from_lastfm(ALBUM, store) == (
        None, {'provider': 'lastfm_album', 'status': 'not_configured'})
