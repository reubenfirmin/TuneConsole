from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.artists import Artists
from yt_playlist.controllers.discovery import Discovery
from yt_playlist.controllers.recommendation_eligibility import RecommendationEligibility
from yt_playlist.core import discovery_policy
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.collection_recovery import RecoveryFiles
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.rec_proposals import RecProposalsRepo
from yt_playlist.repos.settings import SettingsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def discovery():
    repository = create_autospec(DiscoveryRepo, instance=True, spec_set=True)
    artists = create_autospec(Artists, instance=True, spec_set=True)
    eligibility = create_autospec(RecommendationEligibility, instance=True, spec_set=True)
    files = create_autospec(RecoveryFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    proposals = create_autospec(RecProposalsRepo, instance=True, spec_set=True)
    settings = create_autospec(SettingsRepo, instance=True, spec_set=True)
    return Discovery(repository, artists, eligibility, files, proposals=proposals, settings=settings)


def test_starter_identity_is_resolved_before_choice_and_limit_is_rechecked(discovery):
    discovery.artists.ensure_artist.return_value = 7
    discovery.repository.starter_count.return_value = 11
    discovery.repository.starter_artists.return_value = [{'artist_id': 7}]
    assert discovery.select_starter_artist('Name', 'browse', 'image', True, 9) == [{'artist_id': 7}]
    discovery.artists.ensure_artist.assert_called_once_with('Name', provider='youtube', provider_id='browse')
    discovery.repository.set_starter_artist.assert_called_once_with(
        7, 'browse', 'image', True, 9, score=3., maximum=12)


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
    discovery.seed_spotify_artists(3, {'below': 2, 'included': 3, 'higher': 10}, 123, import_key='import')
    assert [call.args for call in discovery.artists.state_id.call_args_list] == [('included',), ('higher',)]
    discovery.repository.seed_spotify_artists.assert_called_once_with(
        3, [(10, 3.), (20, 10.)], 123, import_key='import')


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
    discovery.files.publish.side_effect = lambda *args: steps.append('file') or ('filename', 'digest', 10)
    discovery.repository.store_discovered_artist.side_effect = lambda *args, **kwargs: steps.append('sql')
    discovery.upsert_discovered_artist('Artist', 9, ['because'], ['fits'], 'image', 12, 'Rock', source='spotify_model')
    assert steps == (['pin', 'file', 'sql', 'release'] if native else ['pin', 'sql', 'release'])
    discovery.repository.store_discovered_artist.assert_called_once_with(
        7, 9, 'image', 12, 'Rock', source='spotify_model',
        reference=('filename', 'digest', 10) if native else None, encoded=('["because"]', '["fits"]'))


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
    proposals = create_autospec(RecProposalsRepo, instance=True, spec_set=True)
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
