"""Listening orchestration consumes SQL facts and FAO results without SQLite."""
import hashlib
import json
from contextlib import nullcontext
from itertools import count
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.listening_analytics import ListeningAnalytics
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.core.schema_contract import MigrationError
from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def analytics():
    repository = create_autospec(ListeningAnalyticsRepo, instance=True, spec_set=True)
    repository.output_reference.return_value = None
    repository.reserve_file.side_effect = count(1)
    files = create_autospec(RecommendationFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    files.describe.side_effect = lambda raw: (hashlib.sha256(raw).hexdigest(), len(raw))
    files.publish_reserved.side_effect = lambda identity, raw, **kwargs: ('file', *kwargs['expected'])
    return ListeningAnalytics(repository, files, Mock(), Mock())


def bundle():
    raw = json.dumps({'columns': ['day', 'track'], 'rows': [['2026-10-29', 'song']], 'catalog': {}})
    key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    manifest = json.dumps({'schema': 2, 'timezone': 'UTC', 'chunks': [
        {'key': key, 'from': '2026-10-29', 'until': '2026-10-29', 'bytes': len(raw.encode())}]})
    return {key: raw}, manifest, '{"id":"edition","stories":[]}'


def test_selected_native_document_loads_after_detaching_its_sql_reference(analytics):
    analytics.repository.output_reference.return_value = ('file', 'digest', 100)
    analytics.read_file.return_value = '{"id":"edition"}'
    assert analytics.prepared('listening_edition') == {'id': 'edition'}
    analytics.repository.output_reference.assert_called_once_with('listening_edition', '')
    analytics.read_file.assert_called_once_with('file', 'digest', 100, surface='listening_edition', chunk_key='')


def test_chunk_identity_is_normalized_before_the_sql_lookup(analytics):
    key = 'a' * 24
    analytics.encoded('listening_chunk:' + key)
    analytics.repository.output_reference.assert_called_once_with('listening_chunk', key)


@pytest.mark.parametrize('exists', [False, True])
def test_presence_checks_detached_filename_without_decoding_content(analytics, exists):
    analytics.repository.output_reference.return_value = ('document', 'digest', 100)
    analytics.exists_file.return_value = exists
    assert analytics.present('listening_edition') is exists
    analytics.exists_file.assert_called_once_with('document')
    analytics.read_file.assert_not_called()


@pytest.mark.parametrize('key', ['', 'a' * 23, 'A' * 24, '../' + 'a' * 21])
def test_invalid_chunk_keys_do_not_reach_sql_or_files(analytics, key):
    assert analytics.encoded('listening_chunk:' + key) is None
    analytics.repository.output_reference.assert_not_called()
    analytics.read_file.assert_not_called()


def test_unknown_surface_is_rejected_before_sql(analytics):
    with pytest.raises(ValueError, match='Unknown listening output'):
        analytics.encoded('other')
    analytics.repository.output_reference.assert_not_called()


def test_missing_selected_output_is_a_cache_miss(analytics):
    assert analytics.prepared() is None
    assert not analytics.present('listening_analytics')
    analytics.read_file.assert_not_called()
    analytics.exists_file.assert_not_called()


@pytest.mark.parametrize('error', [OSError('missing'), ValueError('corrupt'), MigrationError('wrong digest')])
def test_unreadable_selected_file_is_a_cache_miss(analytics, error):
    analytics.repository.output_reference.return_value = ('file', 'digest', 100)
    analytics.read_file.side_effect = error
    assert analytics.encoded() is None
    analytics.repository.output_reference.assert_called_once()


def test_generation_writes_every_file_before_the_guarded_sql_publication(analytics):
    chunks, manifest, edition = bundle()
    generation = analytics.prepare_generation(chunks, manifest, edition, 123, input_revision=17)
    assert analytics.files.publish_reserved.call_count == 3
    assert generation.file_ids == (1, 2, 3)
    analytics.repository.publish_files.assert_not_called()
    assert generation.publication.input_revision == 17
    assert generation.publication.timezone == 'UTC'
    assert [row[:2] for row in generation.durable] == [
        ('listening_chunk', next(iter(chunks))), ('listening_analytics', ''), ('listening_edition', '')]
    assert all(row[-1] == 123 for row in generation.durable)
    analytics.files.publish_reserved.reset_mock()
    analytics.publish_generation(generation, 'UTC')
    analytics.repository.publish_files.assert_called_once_with(
        generation.publication, generation.durable, keep_previous=True, timezone='UTC', file_ids=(1, 2, 3))
    analytics.files.publish_reserved.assert_not_called()
    analytics.read_file.assert_not_called()


def test_partial_file_failure_keeps_sql_publication_untouched_and_can_be_retried(analytics):
    chunks, manifest, edition = bundle()
    analytics.files.publish_reserved.side_effect = [('first', 'digest', 10), OSError('volume unavailable')]
    with pytest.raises(OSError, match='volume unavailable'):
        analytics.prepare_generation(chunks, manifest, edition, 123, input_revision=17)
    analytics.repository.publish_files.assert_not_called()
    analytics.files.publish_reserved.side_effect = None
    analytics.files.publish_reserved.return_value = ('file', 'digest', 10)
    generation = analytics.prepare_generation(chunks, manifest, edition, 123, input_revision=17)
    analytics.publish_generation(generation, 'UTC')
    analytics.repository.publish_files.assert_called_once()


def test_invalid_generation_is_rejected_before_any_file_is_written(analytics):
    chunks, manifest, edition = bundle()
    with pytest.raises(ValueError, match='missing selected listening chunk'):
        analytics.prepare_generation({}, manifest, edition, 123, input_revision=17)
    analytics.files.publish_reserved.assert_not_called()
    analytics.repository.reserve_file.assert_not_called()
    analytics.repository.publish_files.assert_not_called()


@pytest.mark.parametrize('surface', ['listening_analytics', 'listening_edition'])
def test_refreshing_one_native_document_retains_the_other_document_and_indexed_chunks(analytics, surface):
    chunks, manifest, edition = bundle()
    raw_by_surface = {'listening_analytics': manifest, 'listening_edition': edition,
                     **{'listening_chunk:' + key: raw for key, raw in chunks.items()}}
    analytics.repository.output_reference.side_effect = lambda name, key: (
        name + ':' + key if key else name, 'digest', 100)
    analytics.read_file.side_effect = lambda filename, *args, **kwargs: raw_by_surface[filename]
    analytics.publish(raw_by_surface[surface], 200, surface)
    publication, durable = analytics.repository.publish_files.call_args.args
    assert publication.memberships[0][1] == next(iter(chunks))
    assert {row[0] for row in durable} == {'listening_chunk', 'listening_analytics', 'listening_edition'}
    assert analytics.repository.publish_files.call_args.kwargs == {'keep_previous': True, 'file_ids': (1, 2, 3)}


def test_native_chunks_require_a_complete_generation(analytics):
    with pytest.raises(ValueError, match='complete manifest'):
        analytics.publish('{}', 123, 'listening_chunk:' + 'a' * 24)
    analytics.files.publish_reserved.assert_not_called()
    analytics.repository.publish_files.assert_not_called()


def test_editorial_exposure_supplies_the_six_hour_cutoff(analytics):
    analytics.record_impressions({'story', 'family:deeper'}, 100000)
    analytics.repository.record_impressions.assert_called_once_with(
        {'story', 'family:deeper'}, 100000, last_shown_before=78400)


def test_feedback_interpretation_supplies_bans_as_negative_evidence(analytics):
    batches = iter(([{'item_key': 'song', 'kind': 'dislike', 'at': 1}],))
    analytics.repository.feedback.return_value = batches
    assert analytics.feedback(123) is batches
    analytics.repository.feedback.assert_called_once_with(
        123, kinds=('like', 'dislike', 'ban'), positive='like', negative='dislike')


def test_player_selection_preserves_the_lazy_bounded_sql_stream(analytics):
    batches = iter(([{'kind': 'tick'}],))
    analytics.repository.player.return_value = batches
    assert analytics.player(123) is batches
    analytics.repository.player.assert_called_once_with(
        123, kinds=('listen_start', 'tick', 'state', 'volume', 'track_exit', 'ended', 'bye'))
