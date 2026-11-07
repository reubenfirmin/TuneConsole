from copy import deepcopy
from contextlib import nullcontext
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.collection import Collection
from yt_playlist.controllers.albums import Albums
from yt_playlist.repos.collection import CollectionRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def collection():
    repository = create_autospec(CollectionRepo, instance=True, spec_set=True)
    repository.membership_transaction.side_effect = nullcontext
    return Collection(repository, create_autospec(Albums, instance=True, spec_set=True))


def occurrence(identity=1, occurrence=1, key='song', **values):
    return {**dict(id=identity, title='LP', artist='Artist', browse=f'B{identity}', year=None,
        genre=None, thumbnail=None, saved=False, complete=True, expected_track_count=2,
        occurrence=occurrence, key=key, is_full=True), **values}


@pytest.mark.parametrize('inputs,expected', [
    ({'layout': 'missing'}, None),
    ({'layout': 'frozen', 'title': 'Original', 'videos': ['first', 'first', 'last']},
        {'title': 'Original', 'video_ids': ['first', 'first', 'last']}),
    ({'layout': 'occurrences', 'title': 'LP', 'items': [
        {'position': 0, 'video_id': 'first'}, {'position': 1, 'video_id': 'last'}]},
        {'title': 'LP', 'video_ids': ['first', 'last']}),
    ({'layout': 'occurrences', 'title': 'LP', 'items': []}, None),
    ({'layout': 'occurrences', 'title': 'LP', 'items': [{'position': 1, 'video_id': 'first'}]}, None),
    ({'layout': 'occurrences', 'title': 'LP', 'items': [
        {'position': 0, 'video_id': 'first'}, {'position': 2, 'video_id': 'last'}]}, None),
    ({'layout': 'occurrences', 'title': 'LP', 'items': [{'position': 0, 'video_id': None}]}, None),
])
def test_share_preserves_frozen_order_and_requires_every_live_source(collection, inputs, expected):
    collection.repository.album_share_inputs.return_value = inputs
    before = deepcopy(inputs)
    assert collection.cached_album_share('release') == expected
    collection.repository.album_share_inputs.assert_called_once_with('release')
    assert inputs == before


@pytest.mark.parametrize('occurrences', [True, False])
def test_unordered_fetch_keeps_only_exact_source_links_and_preserves_unknown_keys(collection, occurrences):
    collection.albums.observe.return_value = 12
    collection.repository.tracklist_inputs.return_value = {'occurrences': occurrences, 'keys': {
        'known': {'recording_id': 1, 'entries': [4]},
        'ambiguous': {'recording_id': 2, 'entries': [5, 6]}}}
    collection.repository.unreferenced_tracklists.return_value = []
    collection.cache_album_tracklist('B', 'LP', 'Artist', None, iter(['known', 'known', 'ambiguous', 'unknown']))
    args, options = collection.repository.store_album_tracklist.call_args
    key_field = 'raw_identity_key' if occurrences else 'legacy_track_key'
    assert args == (12, 'B', [*([{'entry_id': 4}] * (2 if occurrences else 1)),
        {'recording_id': 2, key_field: 'ambiguous'}, {'recording_id': None, key_field: 'unknown'}])
    assert options['ordered'] is False
    assert options['complete'] is True
    assert options['expected_count'] == (4 if occurrences else 3)


def test_ordered_fetch_preserves_repeated_occurrences_and_does_not_reuse_an_old_share(collection):
    collection.albums.observe.return_value = 12
    collection.repository.tracklist_inputs.return_value = {'occurrences': True, 'keys': {}}
    collection.repository.unreferenced_tracklists.return_value = []
    entries = [{'entry_id': 7, 'position': 99}, {'entry_id': 7, 'disc_number': 2, 'track_number': 1}]
    before = deepcopy(entries)
    collection.cache_album_tracklist('B', 'LP', 'Artist', None, [], entries=entries, video_ids=['old'])
    args, options = collection.repository.store_album_tracklist.call_args
    assert args == (12, 'B', [{'entry_id': 7, 'position': 0},
        {'entry_id': 7, 'disc_number': 2, 'track_number': 1, 'position': 1}])
    assert options['ordered'] is True
    assert options['complete'] is True
    assert entries == before
    collection.repository.store_album_share.assert_not_called()


def test_partial_fetch_cannot_select_a_complete_publication_and_retains_two_revisions_per_state(collection):
    collection.repository.uses_occurrences.return_value = True
    collection.albums.observe.return_value = 12
    collection.repository.unreferenced_tracklists.return_value = [
        {'id': 9, 'status': 'complete'}, {'id': 8, 'status': 'complete'}, {'id': 7, 'status': 'complete'},
        {'id': 6, 'status': 'partial'}, {'id': 5, 'status': 'partial'}, {'id': 4, 'status': 'partial'}]
    collection.cache_partial_album('B', 'LP', 'Artist', None, [{'entry_id': 7}], expected_count=10)
    args, options = collection.repository.store_album_tracklist.call_args
    assert args == (12, 'B', [{'entry_id': 7, 'position': 0}])
    assert options['complete'] is False
    assert options['expected_count'] == 10
    collection.repository.delete_tracklists.assert_called_once_with([7, 4])
    collection.repository.store_album_share.assert_not_called()


def test_pre_occurrence_layout_ignores_partial_fetches(collection):
    collection.repository.uses_occurrences.return_value = False
    collection.cache_partial_album('B', 'LP', 'Artist', None, [{'entry_id': 7}])
    collection.repository.membership_transaction.assert_not_called()
    collection.albums.observe.assert_not_called()


def test_taste_coverage_counts_recordings_once_and_keeps_memoized_inputs_unchanged(collection):
    inputs = {'tracks': [occurrence(), occurrence(occurrence=2),
                         occurrence(occurrence=3, key='extra', is_full=False)], 'days': {'song': 2, 'extra': 1}}
    collection.repository.taste_album_inputs.return_value = inputs
    before = deepcopy(inputs)
    album, = collection.taste_album_contexts()
    assert album['tracklist_complete'] is True
    assert album['keys'] == ['song']
    assert album['track_days'] == {'song': 2}
    assert album['return_coverage'] == 1
    assert inputs == before
    assert collection.taste_album_contexts() == [album]


@pytest.mark.parametrize('change', [
    {'complete': False}, {'expected_track_count': 3}, {'key': None}])
def test_incomplete_tracklists_never_supply_a_coverage_denominator(collection, change):
    rows = [occurrence(**{key: value for key, value in change.items() if key != 'key'}),
            {**occurrence(occurrence=2, key='other'), **change}]
    collection.repository.taste_album_inputs.return_value = {'tracks': rows, 'days': {'song': 3}}
    album, = collection.taste_album_contexts()
    assert album['tracklist_complete'] is False
    assert album['return_coverage'] is None


def test_taste_requires_saved_return_listening_or_explicit_inclusion(collection):
    rows = [{**occurrence(identity=identity), 'complete': False, 'key': f'key{identity}', 'saved': identity == 2}
            for identity in (4, 3, 2, 1)]
    collection.repository.taste_album_inputs.return_value = {
        'tracks': rows, 'days': {'key1': 1, 'key3': 2}}
    assert [album['id'] for album in collection.taste_album_contexts()] == [2, 3]
    assert [album['id'] for album in collection.taste_album_contexts(include_browse=iter(['B4']))] == [2, 3, 4]


@pytest.mark.parametrize('playlist_id,playlist,albums,browse,expected', [
    ('PL', {'id': 7, 'title': ''}, [{'browse': 'B', 'title': 'LP'}], 'B',
        {'kind': 'playlist', 'id': 7, 'title': 'Playlist'}),
    ('radio-queue', None, [{'browse': 'B', 'title': 'LP'}], 'B', None),
    ('OLAK5uy_queue', None, [{'browse': 'B', 'title': ''}], None,
        {'kind': 'album', 'browse': 'B', 'title': 'Album'}),
    (None, None, [{'browse': 'A', 'title': 'One'}, {'browse': 'B', 'title': 'Two'}], None, None),
    (None, None, [], 'unknown', {'kind': 'album', 'browse': 'unknown', 'title': 'Album'}),
    (None, None, [], None, None)])
def test_playback_source_respects_actual_context_and_ambiguous_video_membership(
        collection, playlist_id, playlist, albums, browse, expected):
    collection.repository.playback_source_inputs.return_value = {'playlist': playlist, 'albums': albums}
    assert collection.playback_source(playlist_id, 'video', browse) == expected
    collection.repository.playback_source_inputs.assert_called_once_with(playlist_id, 'video', browse)


def test_alias_candidates_normalize_titles_and_empty_demand_avoids_catalog_reads(collection):
    assert collection.album_alias_candidates([]) == []
    collection.repository.album_title_rows.assert_not_called()
    collection.repository.album_title_rows.return_value = [
        {'browse_id': 'A', 'title': '  The   Album\n'}, {'browse_id': 'B', 'title': 'Other'}]
    assert collection.album_alias_candidates(['the album']) == [{'browse_id': 'A', 'title': '  The   Album\n'}]


def test_alias_claim_supplies_ordered_ids_and_explicit_retry_and_retention_cutoffs(collection):
    assert collection.claim_album_alias_check('A', 'A', 10000) is False
    collection.repository.claim_album_alias_check.assert_not_called()
    collection.repository.claim_album_alias_check.return_value = True
    assert collection.claim_album_alias_check('B', 'A', 10000) is True
    collection.repository.claim_album_alias_check.assert_called_once_with(
        'A', 'B', 10000, expire_before=10000 - 30 * 86400, retry_before=9100)


def test_collection_albums_composes_artwork_unique_playlists_and_listening_order(collection):
    rows = [dict(album_id=1, album='One', artist='Artist', browse='B1', key='a', thumb=None, pls='2,3,2'),
            dict(album_id=1, album='One', artist='Artist', browse='B1', key='b', thumb='/cover', pls='3,'),
            dict(album_id=2, album='Two', artist='Artist', browse='B2', key='c', thumb=None, pls=None)]
    collection.repository.collection_album_inputs.return_value = {'tracks': rows, 'plays': {'a': 2, 'b': 1, 'c': 4}}
    assert collection.collection_albums() == [
        dict(album='Two', artist='Artist', browse='B2', thumb=None, songs=1, plays=4, n_pls=0),
        dict(album='One', artist='Artist', browse='B1', thumb='/cover', songs=2, plays=3, n_pls=2)]


def test_artist_collection_deduplicates_recordings_across_releases_without_merging_homonyms(collection):
    rows = [dict(artist_id=1, artist='Same', browse='UC1', key='song', album_id=10,
                 album_browse='B1', thumb=None, pls='2,3'),
            dict(artist_id=1, artist='Same', browse='UC1', key='song', album_id=11,
                 album_browse='B2', thumb='/track', pls='3')]
    saved = [dict(artist_id=1, artist='Same', browse='UC1', album_id=11, thumbnail='/album'),
             dict(artist_id=2, artist='Same', browse='UC2', album_id=12, thumbnail='/other')]
    inputs = {'tracks': rows, 'saved': saved, 'plays': {'song': 4}}
    before = deepcopy(inputs)
    collection.repository.collection_artist_inputs.return_value = inputs
    assert collection.collection_artists() == [
        dict(artist='Same', artist_id=1, browse='UC1', plays=4, thumbnail='/track', songs=1, n_albums=2, n_pls=2),
        dict(artist='Same', artist_id=2, browse='UC2', plays=0, thumbnail='/other', songs=0, n_albums=1, n_pls=0)]
    assert inputs == before


def test_artist_page_includes_singles_only_from_persisted_membership_evidence(collection):
    inputs = {'albums': [dict(id=1, album='LP', browse='B', year=None, thumb=None, saved=1)],
        'singles': True, 'tracks': [
            dict(album_id=1, identity_key='song', thumb='/song', pls='1,2'),
            dict(album_id=None, identity_key='single', thumb=None, pls='2'),
            dict(album_id=99, identity_key='historical-label', thumb=None, pls=None)],
        'plays': {'song': 2, 'single': 3, 'historical-label': 100}}
    before = deepcopy(inputs)
    collection.repository.artist_album_inputs.return_value = inputs
    assert collection.artist_albums('Artist', 'UC') == [
        dict(id=None, album='Singles / no album', browse=None, year=None, thumb=None, saved=False,
             songs=1, plays=3, n_pls=1),
        dict(id=1, album='LP', browse='B', year=None, thumb='/song', saved=True, songs=1, plays=2, n_pls=2)]
    collection.repository.artist_album_inputs.assert_called_once_with('Artist', 'UC')
    assert inputs == before
    inputs['singles'] = False
    assert [album['browse'] for album in collection.artist_albums('Artist')] == ['B']


def test_sync_only_replaces_active_accounts_with_the_captured_revision(collection):
    collection.repository.snapshot_inputs.return_value = {
        1: {'active': True, 'revision': 3}, 2: {'active': False, 'revision': 3},
        3: {'active': True, 'revision': 4}, 4: {'active': True, 'revision': 0}}
    collection.repository.legacy_cohort_inputs.return_value = {'has_legacy': True, 'count': 2, 'missing': 1}
    collection.albums.observe.return_value = 20
    snapshots = {1: [{'browse': 'A'}], 2: [{'browse': 'inactive'}], 3: [], 4: [], 99: []}
    assert collection.replace_saved_album_snapshots(snapshots, expected_revisions={1: 3, 2: 3, 3: 3}, now=100) == {1}
    collection.repository.delete_album_memberships.assert_called_once_with(1)
    collection.albums.observe.assert_called_once_with('A', title=None, artist=None, year=None, type=None,
                                                   thumbnail=None, credits=None, rank=2)
    collection.repository.save_album_membership.assert_called_once_with(20, 1)
    collection.repository.store_snapshot_observation.assert_called_once_with(1, 100)


@pytest.mark.parametrize('count,missing,retired', [(0, 0, False), (2, 1, False), (2, 0, True)])
def test_legacy_retirement_requires_an_observed_nonempty_frozen_cohort(collection, count, missing, retired):
    collection.repository.snapshot_inputs.return_value = {1: {'active': True, 'revision': 5}}
    collection.repository.legacy_cohort_inputs.return_value = {'has_legacy': True, 'count': count, 'missing': missing}
    assert collection.replace_saved_album_snapshots({1: []}, now=100) == {1}
    assert collection.repository.delete_album_memberships.call_args_list == (
        [((1,), {}), ((None,), {})] if retired else [((1,), {})])
    collection.repository.store_snapshot_observation.assert_called_once_with(1, 100)


def test_rejected_snapshot_cannot_count_as_an_observation_or_retire_legacy_membership(collection):
    collection.repository.snapshot_inputs.return_value = {1: {'active': True, 'revision': 5}}
    assert collection.replace_saved_album_snapshots({1: []}, expected_revisions={1: 4}, now=100) == set()
    collection.repository.delete_album_memberships.assert_not_called()
    collection.repository.store_snapshot_observation.assert_not_called()
    collection.repository.legacy_cohort_inputs.assert_not_called()


def test_failed_album_ingestion_cannot_publish_snapshot_observation(collection):
    collection.repository.snapshot_inputs.return_value = {1: {'active': True, 'revision': 5}}
    collection.albums.observe.side_effect = ValueError('invalid album')
    with pytest.raises(ValueError, match='invalid album'):
        collection.replace_saved_album_snapshots({1: [{'browse': 'A'}]}, now=100)
    collection.repository.save_album_membership.assert_not_called()
    collection.repository.store_snapshot_observation.assert_not_called()
    collection.repository.legacy_cohort_inputs.assert_not_called()


@pytest.mark.parametrize('has_legacy', [False, True])
def test_unattributed_addition_resets_existing_cohort_without_enrolling_later_accounts(collection, has_legacy):
    collection.repository.legacy_cohort_inputs.return_value = {'has_legacy': has_legacy, 'count': 2, 'missing': 1}
    collection.albums.observe.return_value = 20
    collection.add_saved_album({'browse': 'A'})
    collection.repository.advance_album_revision.assert_called_once_with(None)
    collection.repository.reset_legacy_cohort.assert_called_once_with(replace=not has_legacy)
    collection.repository.save_album_membership.assert_called_once_with(20, None)


def test_attributed_addition_preserves_legacy_cohort(collection):
    collection.albums.observe.return_value = 20
    collection.add_saved_album({'browse': 'A'}, account_id=1)
    collection.repository.advance_album_revision.assert_called_once_with(1)
    collection.repository.reset_legacy_cohort.assert_not_called()
    collection.repository.legacy_cohort_inputs.assert_not_called()
    collection.repository.save_album_membership.assert_called_once_with(20, 1)


def test_legacy_replacement_only_clears_unattributed_memberships_and_freezes_new_cohort(collection):
    collection.albums.observe.return_value = 20
    collection.replace_saved_albums(iter([{'title': 'unknown'}, {'browse': 'A'}]))
    collection.repository.delete_album_memberships.assert_called_once_with(None)
    collection.repository.reset_legacy_cohort.assert_called_once_with(replace=True)
    collection.albums.observe.assert_called_once_with('A', title=None, artist=None, year=None, type=None,
                                                   thumbnail=None, credits=None, rank=2)
    collection.repository.save_album_membership.assert_called_once_with(20, None)


@pytest.mark.parametrize('now', [float('inf'), float('nan'), True])
def test_invalid_snapshot_time_cannot_open_a_write_unit(collection, now):
    with pytest.raises(ValueError):
        collection.replace_saved_album_snapshots({1: []}, now=now)
    collection.repository.membership_transaction.assert_not_called()


def test_snapshot_rechecks_revision_after_artist_and_album_observations(collection):
    collection.repository.snapshot_inputs.side_effect = [
        {1: {'active': True, 'revision': 5}}, {1: {'active': True, 'revision': 6}}]
    collection.albums.observe.return_value = 20
    assert collection.replace_saved_album_snapshots({1: [{'browse': 'A'}]}, expected_revisions={1: 5}, now=100) == set()
    collection.albums.observe.assert_called_once()
    collection.repository.delete_album_memberships.assert_not_called()
    collection.repository.save_album_membership.assert_not_called()
    collection.repository.store_snapshot_observation.assert_not_called()
