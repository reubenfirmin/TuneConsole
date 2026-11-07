"""Publication projection and ordering checks without a database."""
from unittest.mock import Mock
from contextlib import contextmanager, nullcontext
import hashlib

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.recommendation_publications import RecommendationPublications
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.core import recommendation_outputs, recommendation_plan, recommendation_pools
from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def publications():
    service = RecommendationPublications(Mock(spec=RecommendationPublicationsRepo), Mock(), Mock(), Mock(),
                                        Mock(spec=RecommendationFiles))
    service.query.group_candidates.return_value = {}
    service.discovery.candidate_tracks_by_keys.return_value = {}
    service.modes.years_for.return_value = {}
    service.repository.candidate_details.return_value = {}
    service.repository.candidate_play_counts.return_value = {}
    service.files.display.return_value = {}
    service.files.retained.side_effect = nullcontext
    service.repository.staged_slots.return_value = {}
    service.repository.staged_outputs.return_value = {}
    return service


def row(key, rank=0):
    return {'key': key, 'rank': rank, 'recording_id': rank + 1, 'score': .5, 'pool_policy': 'policy'}


def test_missing_upload_advances_cursor_before_hydration(publications):
    publications.repository.candidate_rows.return_value = (7, [
        ('all', 'fresh', {'id': 8, 'eligible_count': 3}, [row('missing', 1)])])
    page = publications.candidate_page('home', 'all', 'fresh', after=0, limit=1, include_display=False)
    assert page == {'generation': 7, 'after': 1, 'exhausted': False, 'tracks': []}
    publications.files.display.assert_not_called()


def test_overlapping_pages_share_catalog_reads_and_keep_separate_attribution(publications):
    publications.repository.candidate_rows.return_value = (7, [
        ('all', 'fresh', {'id': 8, 'eligible_count': 1}, [row('one')]),
        ('rock', 'wheelhouse', {'id': 9, 'eligible_count': 1}, [row('one')])])
    publications.query.group_candidates.return_value = {'one': {'video_id': 'video', 'title': 'Current'}}
    publications.discovery.candidate_tracks_by_keys.return_value = {'one': {'video_id': 'other'}}
    publications.repository.candidate_details.return_value = {'video': {'duration': 123}}
    publications.repository.candidate_play_counts.return_value = {'one': 3}
    publications.modes.years_for.return_value = {'one': '2001-09'}
    publications.files.display.side_effect = [{0: {'reason': 'First', 'lane': 'owned'}}, {}]
    pages = publications.candidate_pages('home', [('all', 'fresh', -1), ('rock', 'wheelhouse', -1)])
    publications.query.group_candidates.assert_called_once_with(['one'])
    publications.repository.candidate_details.assert_called_once_with(['video'])
    first, second = [page['tracks'][0] for page in pages]
    assert (first['video_id'], first['year'], first['duration'], first['plays'], first['_radio_fresh']) == (
        'video', 2001, 123, 3, False)
    assert (first['reason'], first['lane'], second['reason'], second['lane']) == ('First', 'owned', '', 'wheelhouse')
    assert first['serving_context']['recommendation_pool_id'] == 8
    assert second['serving_context']['recommendation_pool_id'] == 9


def test_private_planning_cache_ends_on_success_and_failure(publications):
    publications.discovery.candidate_tracks_by_keys.return_value = {'one': {'video_id': 'first', 'year': 'unknown'}}
    with pytest.raises(RuntimeError), publications.planning_read():
        assert publications._candidate_facts(['one'])['one']['year'] is None
        publications.discovery.candidate_tracks_by_keys.return_value = {'one': {'video_id': 'next'}}
        assert publications._candidate_facts(['one'])['one']['video_id'] == 'first'
        raise RuntimeError('cancelled planning')
    with publications.planning_read():
        assert publications._candidate_facts(['one'])['one']['video_id'] == 'next'
    assert publications._facts_cache is None
    assert publications.discovery.candidate_tracks_by_keys.call_count == 2


def test_cached_missing_candidate_is_rechecked_in_next_pass(publications):
    with publications.planning_read():
        assert publications._candidate_facts(['one']) == {'one': None}
        assert publications._candidate_facts(['one']) == {'one': None}
    publications.discovery.candidate_tracks_by_keys.assert_called_once_with(['one'])
    publications.discovery.candidate_tracks_by_keys.return_value = {'one': {'video_id': 'new', 'year': '２０１２'}}
    assert publications._candidate_facts(['one'])['one']['year'] is None


def test_metadata_cursor_covers_missing_recordings(publications):
    publications.repository.recording_metadata_rows.return_value = (7, [
        {'id': 5, 'identity_key': 'missing'}, {'id': 8, 'identity_key': 'new'}])
    publications.query.group_candidates.return_value = {}
    publications.discovery.discovered_tracks_by_keys.return_value = {'new': {'video_id': 'video'}}
    page = publications.recording_metadata_page('home', after=2, limit=2)
    assert page['after'] == 8 and not page['exhausted']
    assert page['tracks'] == [{'key': 'new', 'recording_id': 8, 'video_id': 'video',
                              'serving_context': {'recommendation_generation_id': 7}}]
    publications.discovery.discovered_tracks_by_keys.assert_called_once_with(['missing', 'new'], available_only=True)


def test_empty_metadata_page_does_not_query_catalog(publications):
    publications.repository.recording_metadata_rows.return_value = (None, [])
    assert publications.recording_metadata_page('home', after=12) == {
        'generation': None, 'after': 12, 'exhausted': True, 'tracks': []}
    publications.query.group_candidates.assert_not_called()
    publications.discovery.discovered_tracks_by_keys.assert_not_called()


def test_output_files_are_read_after_capturing_owner(publications):
    events = []
    reference = {'filename': 'opaque', 'content_digest': 'digest', 'byte_size': 10}
    publications.repository.output_reference.side_effect = lambda *args, **kwargs: events.append('sql') or reference
    publications.files.read.side_effect = lambda *args, **kwargs: events.append('file') or {'result': True}
    assert publications.output('home', 'chunk', generation=7) == {'result': True}
    assert events == ['sql', 'file']
    publications.files.read.assert_called_once_with(reference, surface='home', chunk_key='chunk')
    publications.repository.output_reference.side_effect = None
    publications.repository.output_reference.return_value = None
    assert publications.output('home', 'missing') is None
    assert publications.files.read.call_count == 1


def test_exact_selection_reuses_projection_without_reordering_ranks(publications):
    publications.repository.selected_rows.return_value = (7, [
        ('all', 'fresh', {'id': 8}, [row('two', 2), row('one', 4)])])
    publications.discovery.candidate_tracks_by_keys.return_value = {
        'one': {'video_id': 'one'}, 'two': {'video_id': 'two'}}
    selected = publications.selected_candidates('home', [('all', 'fresh', ['one', 'two'])])
    assert [track['key'] for track in selected[0]] == ['two', 'one']
    assert [track['pool_rank'] for track in selected[0]] == [2, 4]


def test_page_budget_rejects_before_catalog_or_repository(publications):
    with pytest.raises(ValueError, match='16 pages'):
        publications.candidate_pages('home', [('all', 'fresh', -1)] * 17)
    with pytest.raises(ValueError, match='read budget'):
        publications.selected_candidates('home', [('all', 'fresh', ['one'] * 513)])
    publications.repository.candidate_rows.assert_not_called()
    publications.repository.selected_rows.assert_not_called()
    publications.query.group_candidates.assert_not_called()


def output_plan(count=1):
    return recommendation_plan.Plan('test', 'policy', None, 0, 100, outputs=tuple(
        recommendation_plan.Output('home', str(index), recommendation_outputs.encode('home', str(index), {'n': index}))
        for index in range(count)))


def build(plan):
    return recommendation_plan.Build(7, 'worker', recommendation_plan.validate(plan))


def test_output_batches_publish_before_owners_with_file_lifetime_through_commit(publications):
    plan = output_plan(17)
    events, retained = [], False

    @contextmanager
    def lease():
        nonlocal retained
        retained = True
        try:
            yield
        finally:
            retained = False

    def publish(encoded):
        assert retained
        events.append('file')
        return ('path', hashlib.sha256(encoded).hexdigest(), len(encoded))

    def commit(plan, build, files):
        assert retained
        events.append(('owners', len(files)))

    publications.files.retained.side_effect = lease
    publications.files.publish.side_effect = publish
    publications.repository.record_outputs.side_effect = commit
    publications.repository.ready.side_effect = lambda *args: events.append('ready')
    assert publications.stage(plan, build(plan))
    assert events == ['file'] * 16 + [('owners', 16), 'file', ('owners', 1), 'ready']
    assert not retained


def test_cancelled_staging_retains_committed_prefix_and_resumes_without_republishing(publications):
    plan = output_plan(17)
    current = Mock(side_effect=[True, False])
    assert not publications.stage(plan, build(plan), still_current=current)
    assert publications.files.publish.call_count == 16
    publications.repository.ready.assert_not_called()
    publications.repository.staged_outputs.side_effect = [
        {(output.surface, output.chunk_key): hashlib.sha256(output.encoded).hexdigest() for output in plan.outputs[:16]}, {}]
    assert publications.stage(plan, build(plan))
    assert publications.files.publish.call_count == 17
    assert publications.repository.record_outputs.call_count == 2


def test_file_failure_leaves_manifest_and_ready_unwritten(publications):
    plan = output_plan()
    publications.files.publish.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        publications.stage(plan, build(plan))
    publications.repository.record_outputs.assert_not_called()
    publications.repository.ready.assert_not_called()


def test_failed_manifest_keeps_head_unacknowledged(publications):
    plan = output_plan()
    publications.repository.record_outputs.side_effect = RuntimeError('write failed')
    with pytest.raises(RuntimeError, match='write failed'):
        publications.stage(plan, build(plan))
    publications.files.publish.assert_called_once_with(plan.outputs[0].encoded)
    publications.repository.ready.assert_not_called()
    publications.repository.publish.assert_not_called()


def test_pool_resume_appends_bounded_prefix_then_verifies_before_attachment(publications):
    pool = recommendation_pools.prepare('fresh', 'policy', None, '1' * 64, 'fresh',
        tuple(recommendation_pools.Candidate(index + 1, .5) for index in range(300)))
    plan = recommendation_plan.Plan('test', 'policy', None, 0, 100,
        slots=(recommendation_plan.Slot('home', 'all', 'fresh', pool),))
    publications.repository.begin_pool.return_value = (9, False, None)
    publications.repository.pool_size.return_value = 128
    events = []
    publications.repository.append_pool.side_effect = lambda *args: events.append(('append', len(args[2]), args[3]))
    publications.repository.verify_pool.side_effect = lambda *args: events.append('verify')
    publications.repository.attach_pool.side_effect = lambda *args: events.append('attach')
    assert publications.stage(plan, build(plan))
    assert events == [('append', 128, 128), ('append', 44, 256), 'verify', 'attach']


def test_failed_pool_verification_does_not_attach_or_mark_ready(publications):
    pool = recommendation_pools.prepare('fresh', 'policy', None, '1' * 64, 'fresh', [])
    plan = recommendation_plan.Plan('test', 'policy', None, 0, 100,
        slots=(recommendation_plan.Slot('home', 'all', 'fresh', pool),))
    publications.repository.begin_pool.return_value = (9, True, None)
    publications.repository.verify_pool.side_effect = ValueError('incomplete membership')
    with pytest.raises(ValueError, match='incomplete membership'):
        publications.stage(plan, build(plan))
    publications.repository.attach_pool.assert_not_called()
    publications.repository.ready.assert_not_called()


def test_changed_plan_is_rejected_before_any_staging_write(publications):
    plan = output_plan()
    with pytest.raises(ValueError, match='staged plan changed'):
        publications.stage(plan, recommendation_plan.Build(7, 'worker', 'different'))
    publications.repository.staged_outputs.assert_not_called()
    publications.repository.ready.assert_not_called()
    publications.files.publish.assert_not_called()
