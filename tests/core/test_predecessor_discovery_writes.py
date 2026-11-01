"""Discovery continues using its installed owners before explicit preparation."""
import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import discovery_sources


pytestmark = pytest.mark.parametrize('installed', [79, 99, 107], indirect=True)


def test_starter_selection_scan_candidates_and_distinct_impressions(installed):
    store, _ = installed
    selected = store.discovery.select_starter_artist('Starter', 'UC-start', 'image', True, 10)
    artist = selected[0]['artist_id']
    assert [row['browse_id'] for row in selected] == ['UC-start']
    store.discovery.select_starter_artist('Starter', 'UC-start', None, True, 11)
    assert len(store.discovery.starter_artists()) == 1
    assert any(row['artist_id'] == artist for row in store.discovery.interested_artists())
    store.discovery.mark_scanned(artist, 20)
    assert not store.discovery.artists_due_for_scan(21, with_ids=True)
    store.discovery.upsert_discovered_artist('Starter', 4, ['retained reason'], ['fit'], None, 25,
                                            artist_id=artist)
    candidate = store.discovery.get_discovered_artists()[0]
    assert candidate['artist_id'] == artist and candidate['because'] == ['retained reason']
    store.discovery.record_discovery_impressions('artist', [artist], 'visit', 30)
    store.discovery.record_discovery_impressions('artist', [artist], 'visit', 31)
    store.discovery.record_discovery_impressions('artist', [artist], 'another-visit', 40)
    candidate = store.discovery.get_discovered_artists()[0]
    assert (candidate['offered_count'], candidate['first_shown'], candidate['last_shown']) == (2, 30, 40)
    store.discovery.prune_discovered(set(), set(), {'Starter'})
    assert store.discovery.starter_artists()
    store.discovery.select_starter_artist('Starter', 'UC-start', None, False, 50)
    assert not store.discovery.starter_artists()
    assert store.discovery.get_discovered_artists()[0]['because'] == []


def test_source_cleanup_and_account_scoped_import_reset(installed):
    store, _ = installed
    first = store.accounts.upsert_account('First', 'first.json', None, True)
    second = store.accounts.upsert_account('Second', 'second.json', None, False)
    store.discovery.seed_spotify_artists(first, {'First only': 3, 'Shared': 5}, 10)
    store.discovery.seed_spotify_artists(second, {'Second only': 4, 'Shared': 8}, 11)
    with store.conn:
        discovery_sources.remove_spotify_artists(store.conn, account_id=second)
    candidates = {row['artist']:row for row in store.discovery.get_discovered_artists()}
    assert candidates.keys() == {'First only', 'Shared'}
    assert candidates['Shared']['score'] == 5
    store.discovery.upsert_discovered_album('MPRE-source', 'Artist', 'Album', 2000, None, 20)
    key = store.discovery.upsert_discovered_track('song|artist', 'upload', 'Song', 'Artist', 'Album', None,
                                                 None, '2000', 'MPRE-source', 21)
    assert store.discovery.discovered_source_tokens('album') == {'MPRE-source'}
    assert store.discovery.discovery_sources_for_track(key)[0]['source_token'] == 'MPRE-source'
    store.discovery.record_discovery_impressions('track', [key], 'track-visit', 30)
    store.discovery.record_discovery_impressions('track', [key], 'track-visit', 31)
    assert store.discovery.get_discovered_tracks()[0]['offered_count'] == 1
    assert store.discovery.remove_discovery_source(key, 'MPRE-source')
    assert not store.discovery.get_discovered_tracks()
    assert not store.discovery.discovered_source_tokens()
