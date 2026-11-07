from contextlib import contextmanager, nullcontext
import json
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.artists import Artists
from yt_playlist.controllers.discovery import Discovery
from yt_playlist.controllers.tracks import Tracks
from yt_playlist.controllers.albums import Albums
from yt_playlist.controllers.genres import Genres
from yt_playlist.controllers.metadata_choices import MetadataChoices
from yt_playlist.controllers.recording_keys import RecordingKeys
from yt_playlist.controllers.retained_originals import RetainedOriginals
from yt_playlist.controllers.recommendation_eligibility import RecommendationEligibility
from yt_playlist.core import discovery_policy
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.collection_recovery import RecoveryFiles
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.repos.settings import SettingsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def discovery():
    repository = create_autospec(DiscoveryRepo, instance=True, spec_set=True)
    artists = create_autospec(Artists, instance=True, spec_set=True)
    eligibility = create_autospec(RecommendationEligibility, instance=True, spec_set=True)
    files = create_autospec(RecoveryFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    proposals = create_autospec(RecommendationProposals, instance=True, spec_set=True)
    settings = create_autospec(SettingsRepo, instance=True, spec_set=True)
    originals = create_autospec(RetainedOriginals, instance=True, spec_set=True)
    repository.origin_inputs.return_value = dict(video=None, album=None, artists=set(), account=False)
    return Discovery(repository, artists, eligibility, files, originals=originals, proposals=proposals, settings=settings,
        tracks=create_autospec(Tracks, instance=True, spec_set=True), albums=create_autospec(Albums, instance=True, spec_set=True),
        genres=create_autospec(Genres, instance=True, spec_set=True), metadata=create_autospec(MetadataChoices, instance=True, spec_set=True),
        keys=create_autospec(RecordingKeys, instance=True, spec_set=True))


def test_artist_reconciliation_preserves_explanation_before_clearing_derived_state(discovery):
    candidate = dict(artist_id=7, found_at=2, recovery_filename='file', recovery_digest='digest', recovery_bytes=20)
    discovery.repository.artist_reconciliation_inputs.return_value = dict(typed=True,
        artist_scans=[], discovered_artists=[candidate], sources=[], impressions=[], legacy=[], duplicates=0)
    discovery.repository.origin_merge_inputs.return_value = []
    discovery.files.read.return_value = {'because': ['captured'], 'fits': ['genre']}
    discovery.reconcile_artist(7, 12, source='youtube')
    discovery.files.read.assert_called_once_with(('file', 'digest', 20), kind='discovery-candidate', owner=7)
    archive = json.loads(discovery.originals.retain.call_args.args[3])
    assert archive['explanation'] == {'because': ['captured'], 'fits': ['genre']}
    merged = discovery.repository.apply_artist_reconciliation.call_args.args[-1]
    assert merged == {**candidate, 'artist_id': 12, 'recovery_filename': None, 'recovery_digest': None, 'recovery_bytes': None}
    assert candidate['recovery_filename'] == 'file'


def test_missing_original_explanation_prevents_artist_merge(discovery):
    discovery.repository.artist_reconciliation_inputs.return_value = dict(typed=True, artist_scans=[],
        discovered_artists=[dict(artist_id=7, found_at=2, recovery_filename='file', recovery_digest='digest', recovery_bytes=20)],
        sources=[], impressions=[], legacy=[], duplicates=0)
    discovery.repository.origin_merge_inputs.return_value = []
    discovery.files.read.side_effect = OSError('expired')
    with pytest.raises(OSError, match='expired'):
        discovery.reconcile_artist(7, 12, source='youtube')
    discovery.repository.apply_artist_reconciliation.assert_not_called()


def test_genre_origin_merge_retains_colliding_contributions_before_sql(discovery):
    origin = dict(id=4, kind='genre', genre_key='old')
    track = dict(id=3, recording_id=10, input_video_id='v', first_found_at=1, last_found_at=2)
    artist = dict(artist_id=7, origin_id=4, score=.5)
    inputs = [dict(origin=origin, target=9, sources=[track], artists=[artist])]
    discovery.repository.origin_merge_inputs.return_value = inputs
    discovery.merge_genre_origins('old', 'new')
    assert [call.args[1] for call in discovery.originals.retain.call_args_list] == [
        'discovery_origin_merge', 'discovery_source_origin_merge', 'discovery_artist_origin_merge']
    discovery.repository.apply_origin_merge.assert_called_once_with('genre_key', 'new', inputs)


def test_artist_origin_collision_uses_rebound_sources_without_resurrecting_retired_artist(discovery):
    discovery.repository.artist_reconciliation_inputs.return_value = dict(typed=True, artist_scans=[],
        discovered_artists=[], sources=[], impressions=[], legacy=[], duplicates=0)
    discovery.repository.origin_merge_inputs.return_value = [dict(origin={'id': 4}, target=9, sources=[], artists=[
        dict(artist_id=7, origin_id=4, score=.8), dict(artist_id=12, origin_id=4, score=.2)])]
    discovery.reconcile_artist(7, 12, source='youtube')
    origins = discovery.repository.apply_artist_reconciliation.call_args.args[3]
    assert origins[0]['artists'] == [dict(artist_id=12, origin_id=4, score=.8)]


def test_starter_identity_is_resolved_before_choice_and_limit_is_rechecked(discovery):
    discovery.artists.ensure_artist.return_value = 7
    discovery.repository.starter_count.return_value = 11
    discovery.repository.starter_artists.return_value = [{'artist_id': 7}]
    discovery.repository.explanation_files.return_value = False
    assert discovery.select_starter_artist('Name', 'browse', 'image', True, 9) == [{'artist_id': 7}]
    discovery.artists.ensure_artist.assert_called_once_with('Name', provider='youtube', provider_id='browse')
    discovery.repository.set_starter_artist.assert_called_once_with(
        7, 'browse', 'image', True, 9, score=3., maximum=12, source='artist_start:browse', origin=None)


def test_full_starter_cohort_rejects_new_selection_but_allows_removal(discovery):
    discovery.repository.starter_count.return_value = 12
    with pytest.raises(ValueError, match='up to 12'):
        discovery.select_starter_artist('Name', 'browse', None, True, 0)
    discovery.repository.set_starter_artist.assert_not_called()
    discovery.select_starter_artist('Name', 'browse', None, False, 0)
    assert discovery.repository.set_starter_artist.call_args.args[3] is False


def test_starter_tracks_round_robin_deduplicates_and_caps_each_artist(discovery):
    discovery.eligibility.maximum_duration.return_value = 480
    rows = [{'seed': seed, 'recording_id': f'{seed}-{number}'}
            for seed in ('a', 'b') for number in range(6)]
    rows[6]['recording_id'] = 'a-0'
    discovery.repository.starter_track_rows.return_value = rows
    assert [row['recording_id'] for row in discovery.starter_tracks()] == [
        'a-0', 'a-1', 'b-1', 'a-2', 'b-2', 'a-3', 'b-3']
    discovery.repository.starter_track_rows.assert_called_once_with(480)
    assert len(discovery_policy.starter_tracks([
        {'seed': seed, 'recording_id': f'{seed}-{number}'} for seed in range(12) for number in range(6)])) == 24


def test_scan_priority_uses_interest_for_unscanned_and_respects_cutoff(discovery):
    discovery.repository.interested_artists.return_value = [
        {'artist_id': 1, 'artist': 'Low', 'score': 1},
        {'artist_id': 2, 'artist': 'High', 'score': 9},
        {'artist_id': 3, 'artist': 'Old', 'score': 20},
        {'artist_id': 4, 'artist': 'Current', 'score': 99}]
    discovery.repository.artist_scan_times.return_value = {1: None, 3: 1, 4: 5 * 86400}
    assert discovery.artists_due_for_scan(10 * 86400, budget=2, artist_limit=100) == ['High', 'Low']
    discovery.repository.interested_artists.assert_called_once_with(weights=(1., 2., 3.), limit=100)
    assert [row['artist'] for row in discovery.artists_due_for_scan(10 * 86400, with_ids=True)] == [
        'High', 'Low', 'Old']


def test_artwork_work_is_deduplicated_and_bounded_and_failures_delay_retry(discovery):
    discovery.artist_images_due([1, 1, *range(2, 60)], 100)
    discovery.repository.artist_images_due.assert_called_once_with(list(range(1, 41)), 100)
    discovery.record_artist_image(1, None, 100)
    discovery.repository.record_artist_image.assert_called_once_with(1, None, retry_at=86500)


def test_spotify_threshold_is_applied_before_resolving_identities(discovery):
    discovery.artists.state_id.side_effect = [10, 20]
    discovery.repository.explanation_files.return_value = False
    discovery.seed_spotify_artists(3, {'below': 2, 'included': 3, 'higher': 10}, 123, import_key='import')
    assert [call.args for call in discovery.artists.state_id.call_args_list] == [('included',), ('higher',)]
    discovery.repository.seed_spotify_artists.assert_called_once_with(
        3, [(10, 3.), (20, 10.)], 123, source='spotify_export:3', origin=None, excluded_kinds=('spotify_export',))


@pytest.mark.parametrize('native', [False, True])
def test_artist_explanation_is_prepared_before_sql_and_pinned_during_publication(discovery, native):
    steps = []

    @contextmanager
    def retained():
        steps.append('pin')
        yield
        steps.append('release')

    discovery.files.retained.side_effect = retained
    discovery.artists.state_id.return_value = 7
    discovery.repository.explanation_files.return_value = native
    discovery.genres.resolve.return_value = ('rock', None)
    discovery.files.publish.side_effect = lambda *args: steps.append('file') or ('filename', 'digest', 10)
    discovery.repository.store_discovered_artist.side_effect = lambda *args, **kwargs: steps.append('sql')
    discovery.upsert_discovered_artist('Artist', 9, ['because'], ['fits'], 'image', 12, 'Rock', source='spotify_model')
    assert steps == (['pin', 'file', 'sql', 'release'] if native else ['pin', 'sql', 'release'])
    discovery.repository.store_discovered_artist.assert_called_once_with(
        7, 9, 'image', 12, 'Rock', source='spotify_model',
        reference=('filename', 'digest', 10) if native else None, encoded=('["because"]', '["fits"]'),
        origin=discovery._origin('spotify_model') if native else None, genre_key='rock')


def test_invalid_explanation_cannot_create_identity_or_publish(discovery):
    with pytest.raises(ValueError):
        discovery.upsert_discovered_artist('Artist', 1, [float('nan')], [], None, 1)
    discovery.artists.state_id.assert_not_called()
    discovery.files.publish.assert_not_called()
    discovery.repository.store_discovered_artist.assert_not_called()


def test_explanation_file_failure_cannot_replace_stored_candidate(discovery):
    discovery.repository.explanation_files.return_value = True
    discovery.files.publish.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        discovery.upsert_discovered_artist('Artist', 1, [], [], None, 1)
    discovery.repository.store_discovered_artist.assert_not_called()


def test_expiry_uses_found_clock_for_unshown_tracks_only(discovery):
    discovery.repository.discovery_track_rows.return_value = [
        {'first_shown': None, 'found_at': 3}, {'first_shown': 8, 'found_at': 3}]
    discovery.repository.discovery_album_rows.return_value = [{'first_shown': None}, {'first_shown': 8}]
    discovery.repository.discovery_artist_rows.return_value = [{'first_shown': 0}]
    assert [row['gc_at'] for row in discovery.discovery_track_view(999, 2)] == [172803, 172808]
    assert [row['gc_at'] for row in discovery.discovery_album_view(999, 2)] == [None, 172808]
    assert discovery.discovery_artist_view(999, 2)[0]['gc_at'] == 172800


def test_pruning_keeps_held_tracks_and_explicit_starter_artists(discovery):
    discovery.repository.discovered_track_keys.return_value = {'owned', 'held', 'unowned'}
    discovery.prune_discovered_tracks({'owned', 'held'}, {'held'})
    discovery.repository.delete_pool_entries.assert_called_once_with((), (), {'owned'})
    discovery.repository.orphan_track_keys.return_value = {'orphan', 'held'}
    assert discovery.prune_orphan_discovered_tracks({'held'}) == 1
    discovery.repository.prunable_album_rows.return_value = [
        {'browse_id': 'saved', 'album_id': 1}, {'browse_id': 'owned', 'album_id': 2},
        {'browse_id': 'keep', 'album_id': 3}]
    discovery.repository.prunable_artist_ids.return_value = [5, 6, 7]
    discovery.artists.public_keys.return_value = {5: 'Public', 6: 'Other', 7: 'Keep'}
    discovery.prune_discovered({2}, {'saved'}, {'Public', '@artist:6'})
    discovery.repository.delete_pool_entries.assert_called_with(['saved', 'owned'], [5, 6], ())


def cleanup_snapshot(discovery):
    repository = create_autospec(DiscoveryRepo, instance=True, spec_set=True)
    repository.cleanup_head_rows.return_value = []
    repository.expired_pool_rows.return_value = {'albums': ['album'], 'artists': [7], 'artist_targets': [7],
                                               'tracks': ['gone', 'held', 'generated']}
    repository.cleanup_track_details.return_value = {'sources': {'source'}, 'unowned': {'gone'}}
    eligibility = create_autospec(RecommendationEligibility, instance=True, spec_set=True)
    eligibility.generated_track_keys.return_value = {'generated'}
    proposals = create_autospec(RecommendationProposals, instance=True, spec_set=True)
    proposals.cache_rows.return_value = []
    snapshot = SimpleNamespace(store=SimpleNamespace(discovery=SimpleNamespace(repository=repository),
        rec_eligibility=eligibility, rec_proposals=proposals), release_reader=Mock(),
        publish_ordered=Mock(side_effect=lambda publish: publish() or True))
    discovery.repository.cleanup_snapshot.side_effect = lambda: nullcontext(snapshot)
    discovery.proposals.stage_pruned_cache.return_value = []
    return snapshot, repository


@pytest.mark.parametrize('failure', ['cache', 'checkpoints', 'settings', 'delete', None])
def test_cleanup_commits_in_safe_order_and_stops_at_failed_step(discovery, failure):
    snapshot, repository = cleanup_snapshot(discovery)
    steps = []

    def step(name):
        def run(*args):
            assert snapshot.release_reader.called
            steps.append(name)
            if failure == name:
                raise RuntimeError(name)
        return run

    discovery.proposals.publish_pruned_cache.side_effect = step('cache')
    discovery.repository.remember_source_tokens.side_effect = step('checkpoints')
    discovery.settings.replace_keys.side_effect = step('settings')
    discovery.repository.delete_pool_entries.side_effect = step('delete')
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            discovery.gc_discovery_pool(100 * 86400, 30, {'held'})
    else:
        assert discovery.gc_discovery_pool(100 * 86400, 30, {'held'}) == {'albums': 1, 'artists': 1, 'tracks': 1}
    expected = ['cache', 'checkpoints', 'settings', 'delete']
    assert steps == (expected[:expected.index(failure) + 1] if failure else expected)
    repository.expired_pool_rows.assert_called_once_with(70 * 86400)
    repository.cleanup_track_details.assert_called_once_with({'gone'})
    if steps[-1] == 'delete':
        discovery.repository.delete_pool_entries.assert_called_once_with(['album'], [7], {'gone'})


def test_stale_cleanup_cannot_publish_any_owner(discovery):
    snapshot, _repository = cleanup_snapshot(discovery)
    snapshot.publish_ordered.side_effect = None
    snapshot.publish_ordered.return_value = False
    with pytest.raises(WorkBudgetExceeded, match='inputs changed'):
        discovery.gc_discovery_pool(100 * 86400, 30, ())
    discovery.proposals.publish_pruned_cache.assert_not_called()
    discovery.repository.remember_source_tokens.assert_not_called()
    discovery.settings.replace_keys.assert_not_called()
    discovery.repository.delete_pool_entries.assert_not_called()


def test_discovered_track_uses_catalog_workflow_and_separate_membership_link_and_hints(discovery):
    discovery.tracks.upsert_track.return_value = 7
    discovery.tracks.catalog_subject.side_effect = [{'recording_id': 12}, {'identity_key': '@recording:12|artist'}]
    discovery.repository.explanation_files.return_value = True
    discovery.repository.pooled_album.return_value = True
    discovery.repository.origin_inputs.return_value['album'] = 9
    key = discovery.upsert_discovered_track('imported|artist', 'video', 'Song', 'Artist', 'Album', 'cover',
        'Jazz', '2000', 'release', 10, {'bpm': 120, 'title': 'Unrelated', 'gain': 4}, duration_s=180)
    assert key == '@recording:12|artist'
    discovery.tracks.upsert_track.assert_called_once_with('video', 'Song', 'Artist', 'Album', thumbnail='cover',
        created_at=10, library=False, preferred_key='imported|artist', credits=None, duration_s=180)
    discovery.keys.add_alias.assert_called_once_with('imported|artist', 12, 'discovery')
    discovery.repository.add_track_membership.assert_called_once_with(12, 7, 10)
    origin = discovery.repository.retain_track_source.call_args.args[-1]
    assert origin['kind'] == 'album' and origin['album_id'] == 9
    discovery.albums.link.assert_called_once_with(7, 'release', source='discovery')
    discovery.metadata.fill_import.assert_called_once_with(7, {'genre': 'Jazz', 'mb_year': '2000', 'bpm': 120},
        'discovery_import', hint_source='release', now=10)


def test_discovery_album_is_observed_before_membership_and_artist_lookup_uses_controller(discovery):
    discovery.artists.state_id.return_value = 12
    discovery.albums.observe.return_value = 9
    discovery.upsert_discovered_album('release', 'Artist', 'Album', '2000', 'cover', 10, genre='Jazz')
    discovery.albums.observe.assert_called_once_with('release', title='Album', artist='Artist', year='2000',
        thumbnail='cover', genre='Jazz', artist_id=12, rank=1)
    discovery.repository.add_album_membership.assert_called_once_with(9, 10)
    discovery.artists.resolve.return_value = 12
    discovery.discovered_albums_for_artist('Artist')
    discovery.repository.discovered_albums_for_artist.assert_called_once_with(12)


@pytest.mark.parametrize(('source', 'kind', 'column', 'target'), [
    ('radio:video', 'radio', 'seed_video_id', 'video'),
    ('genre:Jazz', 'genre', 'genre_key', 'jazz'),
    ('artist:channel', 'artist', 'artist_id', 12),
    ('artist_start:channel', 'artist_start', 'artist_id', 12),
    ('release', 'album', 'album_id', 9),
])
def test_origin_resolves_existing_subjects_without_admitting_new_ones(discovery, source, kind, column, target):
    discovery.repository.origin_inputs.return_value.update(video='video', artists={12}, album=9)
    discovery.genres.resolve.return_value = ('jazz', None)
    origin = discovery._origin(source)
    assert origin['kind'] == kind and origin[column] == target
    assert origin['legacy_source_key'] is None and origin['resolution_status'] == 'resolved'
    discovery.artists.ensure_artist.assert_not_called()


def test_origin_keeps_ambiguous_artist_input_unresolved(discovery):
    discovery.repository.origin_inputs.return_value['artists'] = {12, 13}
    origin = discovery._origin('artist:channel')
    assert origin['kind'] == 'legacy_unknown' and origin['artist_id'] is None
    assert origin['legacy_source_key'] == 'artist:channel'
    assert origin['resolution_reason'] == 'missing or ambiguous artist owner'


def test_origin_cannot_assign_an_import_to_a_different_account(discovery):
    with pytest.raises(ValueError, match='account disagrees'):
        discovery._origin('spotify_export:4', account_id=3, import_key='import')
