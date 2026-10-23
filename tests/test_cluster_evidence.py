"""Facts remain reusable outside the serving connection and surface wording."""
from dataclasses import FrozenInstanceError

import pytest

from yt_playlist.rec.cluster_explain import connection_reasons
from yt_playlist.repos.cluster_evidence import (
    AlbumNeighbor, ConnectionEvidence, PlaylistNeighbor, SeedGroup, SeedMatches, SeedSong,
)
from yt_playlist.web.cluster_projection import search_results
from tests.test_cluster_search import _seed_library
from tests.test_connection_explain import _playlist, _track


def test_evidence_detaches_all_values_and_presentation_from_sqlite(store):
    iid, _ = _seed_library(store)
    child = _track(store, 'Child', 'Artist', genre='techno', year='1991')
    path = _track(store, 'Parent', 'Other', genre='techno', year='1999')
    _playlist(store, iid, 'Evidence', [child, path])
    evidence = store.cluster_evidence.connection(child, [path])
    matches = store.cluster_evidence.seed_matches('Ritmo')

    store.conn.close()
    assert evidence.playlists[0].artist == 'Other'
    assert evidence.path_genres == ('techno',)
    assert evidence.path_years == ('1999',)
    assert [r['kind'] for r in connection_reasons(evidence)] == ['playlist', 'genre', 'decade']
    assert search_results(matches)[0]['sub'] == '2 tracks'
    with pytest.raises(FrozenInstanceError):
        evidence.genre = 'different'


def test_explanation_priority_and_bounded_artist_names_do_not_change_evidence():
    evidence = ConnectionEvidence(
        artist='Child', same_artist='Child', genre='techno', year='1991-05-02',
        playlists=(PlaylistNeighbor(1, 'Child'), PlaylistNeighbor(1, ' A '),
                   PlaylistNeighbor(2, 'A'), PlaylistNeighbor(2, 'B'), PlaylistNeighbor(3, 'C')),
        album=AlbumNeighbor('Album', 'A'), same_day=True,
        path_genres=('techno',), path_years=('1999',))
    reasons = connection_reasons(evidence, max_artists=2)
    assert [r['kind'] for r in reasons] == ['same_artist', 'playlist', 'album', 'session', 'genre', 'decade']
    assert reasons[1]['text'] == 'In 3 of your playlists, alongside A, B & 1 more.'
    assert reasons[3]['text'] == 'You played them on the same day.'
    assert reasons[5]['text'] == 'Both from the 1990s.'
    assert 'alongside A, B & C.' in connection_reasons(evidence, max_artists=3)[1]['text']


@pytest.mark.parametrize('year', ['', 'unknown', '19', 'xxxx-01-01'])
def test_missing_or_invalid_year_cannot_support_decade_claim(year):
    evidence = ConnectionEvidence(year=year, path_years=('1999',))
    assert connection_reasons(evidence) == []


def test_search_projection_preserves_kind_and_rank_order_without_mutating_seed_keys():
    matches = SeedMatches(
        artists=(SeedGroup('First', 2, ('a', 'b')), SeedGroup('Second', 1, ('c',))),
        playlists=(SeedGroup('Playlist', 1, ('d',)),), songs=(SeedSong('Song', 'Artist', 'e'),))
    rows = search_results(matches)
    assert [r['label'] for r in rows] == ['First', 'Second', 'Playlist', 'Song']
    assert [r['sub'] for r in rows] == ['2 tracks', '1 track', '1 track', 'Artist']
    rows[0]['keys'].clear()
    assert matches.artists[0].keys == ('a', 'b')


def test_quarantine_and_explicit_adoption_agree_across_all_evidence_reads(store):
    iid, _ = _seed_library(store)
    child = _track(store, 'Child', 'Artist')
    path = _track(store, 'Parent', 'Other')
    pid = _playlist(store, iid, 'Ritmo generated', [child, path], ytm='generated')
    # Add modelled tracks so search would find this playlist without quarantine.
    seed_id = store.conn.execute("SELECT id FROM tracks WHERE title='Spektrum'").fetchone()[0]
    store.playlists.set_playlist_tracks(pid, [seed_id, *[
        store.conn.execute('SELECT id FROM tracks WHERE identity_key=?', (key,)).fetchone()[0]
        for key in (child, path)]])
    store.playlists.mark_generated_playlist('generated')
    store.playlists.set_playlist_group('generated', 'Favorites')

    assert pid in store.rec_eligibility.excluded_playlist_ids()
    assert not store.cluster_evidence.connection(child, [path]).playlists
    assert not store.cluster_evidence.seed_matches('Ritmo generated').playlists

    store.playlists.adopt_generated_playlist('generated')
    assert pid not in store.rec_eligibility.excluded_playlist_ids()
    assert store.cluster_evidence.connection(child, [path]).playlists[0].playlist_id == pid
    assert store.cluster_evidence.seed_matches('Ritmo generated').playlists[0].count == 1


def test_playlist_cap_is_strict_and_duplicate_path_tracks_do_not_inflate_counts(store):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    child = _track(store, 'Child', 'Artist')
    path = _track(store, 'Parent', 'Other')
    _playlist(store, iid, 'Small', [child, path])
    assert store.cluster_evidence.connection(child, [path], max_playlist=1).playlists == ()
    evidence = store.cluster_evidence.connection(child, [child, path, path, ''], max_playlist=2)
    assert len(evidence.playlists) == 1
    assert connection_reasons(evidence)[0]['text'] == 'In 1 of your playlist, alongside Other.'


def test_shared_album_and_same_account_day_are_grounded_separately(store):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    other = store.identities.upsert_identity('other', 'other-cred', None, False)
    child = _track(store, 'Child', 'Artist', album='Album', album_browse_id='release')
    path = _track(store, 'Parent', 'Artist', album='Album', album_browse_id='release')
    store.history.record_history_plays(iid, 43200, [(child, 'Today')], source_timezone='UTC')
    store.history.record_history_plays(other, 43200, [(path, 'Today')], source_timezone='UTC')
    evidence = store.cluster_evidence.connection(child, [path])
    assert evidence.album == AlbumNeighbor('Album', 'Artist')
    assert not evidence.same_day
    store.history.record_history_plays(iid, 43201, [(path, 'Today')], source_timezone='UTC')
    assert store.cluster_evidence.connection(child, [path]).same_day


@pytest.mark.parametrize('query', ['', None, ' . - ', ' \t '])
def test_empty_normalized_query_has_no_matches(store, query):
    assert store.cluster_evidence.seed_matches(query) == SeedMatches()


def test_evidence_repository_is_available_on_pinned_readers(store):
    iid, _ = _seed_library(store)
    child = _track(store, 'Child', 'Artist')
    path = _track(store, 'Parent', 'Other')
    _playlist(store, iid, 'Evidence', [child, path])
    with store.read_snapshot() as snapshot:
        evidence = snapshot.store.cluster_evidence.connection(child, [path])
        matches = snapshot.store.cluster_evidence.seed_matches('Ritmo')
    assert evidence.playlists[0].artist == 'Other'
    assert matches.artists[0].count == 2
