"""Listening orchestration consumes SQL facts and FAO results without SQLite."""
import hashlib
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.listening_analytics import ListeningAnalytics
from yt_playlist.core.schema_contract import MigrationError
from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def analytics():
    repository = create_autospec(ListeningAnalyticsRepo, instance=True, spec_set=True)
    repository.uses_files.return_value = True
    repository.output.return_value = {'source': 'file', 'reference': None}
    publisher = Mock(side_effect=lambda raw: ('file', hashlib.sha256(raw).hexdigest(), len(raw)))
    return ListeningAnalytics(repository, publisher, Mock(), Mock(), Mock())


def bundle():
    raw = json.dumps({'columns': ['day', 'track'], 'rows': [['2026-10-29', 'song']], 'catalog': {}})
    key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    manifest = json.dumps({'schema': 2, 'timezone': 'UTC', 'chunks': [
        {'key': key, 'from': '2026-10-29', 'until': '2026-10-29', 'bytes': len(raw.encode())}]})
    return {key: raw}, manifest, '{"id":"edition","stories":[]}'


def test_selected_native_document_loads_after_detaching_its_sql_reference(analytics):
    analytics.repository.output.return_value = {'source': 'file', 'reference': ('file', 'digest', 100)}
    analytics.read_file.return_value = '{"id":"edition"}'
    assert analytics.prepared('listening_edition') == {'id': 'edition'}
    analytics.repository.output.assert_called_once_with('listening_edition', 'listening_edition', '')
    analytics.read_file.assert_called_once_with('file', 'digest', 100, surface='listening_edition', chunk_key='')
    analytics.read_prepared.assert_not_called()


def test_chunk_identity_is_normalized_before_the_sql_lookup(analytics):
    key = 'a' * 24
    analytics.encoded('listening_chunk:' + key)
    analytics.repository.output.assert_called_once_with('listening_chunk:' + key, 'listening_chunk', key)


@pytest.mark.parametrize('source', ['file', 'prepared'])
@pytest.mark.parametrize('exists', [False, True])
def test_presence_checks_detached_filename_without_decoding_content(analytics, source, exists):
    analytics.repository.output.return_value = {'source': source, 'header': {'filename': 'document'},
                                               'reference': ('document', 'digest', 100)}
    analytics.exists_file.return_value = exists
    assert analytics.present('listening_edition') is exists
    analytics.exists_file.assert_called_once_with('document')
    analytics.read_file.assert_not_called()
    analytics.read_prepared.assert_not_called()


@pytest.mark.parametrize('raw,expected', [(None, False), ('null', False), ('{}', False), ('{"text":"Ready"}', True)])
def test_legacy_presence_uses_installed_bytes(analytics, raw, expected):
    analytics.repository.output.return_value = {'source': 'legacy', 'raw': raw}
    assert analytics.present('listening_edition') is expected
    analytics.exists_file.assert_not_called()


@pytest.mark.parametrize('key', ['', 'a' * 23, 'A' * 24, '../' + 'a' * 21])
def test_invalid_chunk_keys_do_not_reach_sql_or_files(analytics, key):
    assert analytics.encoded('listening_chunk:' + key) is None
    analytics.repository.output.assert_not_called()
    analytics.read_file.assert_not_called()


def test_unknown_surface_is_rejected_before_sql(analytics):
    with pytest.raises(ValueError, match='Unknown listening output'):
        analytics.encoded('other')
    analytics.repository.output.assert_not_called()


@pytest.mark.parametrize('source', ['legacy', 'prepared', 'file'])
def test_missing_selected_output_is_a_cache_miss(analytics, source):
    analytics.repository.output.return_value = {'source': source, 'raw': None, 'header': None, 'reference': None}
    assert analytics.prepared() is None
    analytics.read_file.assert_not_called()
    analytics.read_prepared.assert_not_called()


@pytest.mark.parametrize('error', [OSError('missing'), ValueError('corrupt'), MigrationError('wrong digest')])
@pytest.mark.parametrize('source', ['file', 'prepared'])
def test_unreadable_selected_files_do_not_fall_back_to_stale_legacy_bytes(analytics, source, error):
    analytics.repository.output.return_value = {'source': source, 'raw': 'stale', 'header': {'generation': 3},
                                               'reference': ('file', 'digest', 100)}
    analytics.read_file.side_effect = error
    analytics.read_prepared.side_effect = error
    assert analytics.encoded() is None
    assert analytics.repository.output.call_count == 1


def test_legacy_output_uses_exact_bytes_without_file_reads(analytics):
    raw = '{ "value" : 1 }'
    analytics.repository.output.return_value = {'source': 'legacy', 'raw': raw}
    assert analytics.encoded() == raw
    analytics.read_file.assert_not_called()
    analytics.read_prepared.assert_not_called()


def test_generation_writes_every_file_before_the_guarded_sql_publication(analytics):
    chunks, manifest, edition = bundle()
    generation = analytics.prepare_generation(chunks, manifest, edition, 123, input_revision=17)
    assert analytics.publish_file.call_count == 3
    analytics.repository.publish_files.assert_not_called()
    assert generation.publication.input_revision == 17
    assert generation.publication.timezone == 'UTC'
    assert [row[:2] for row in generation.durable] == [
        ('listening_chunk', next(iter(chunks))), ('listening_analytics', ''), ('listening_edition', '')]
    assert all(row[-1] == 123 for row in generation.durable)
    analytics.publish_file.reset_mock()
    analytics.publish_generation(generation, 'UTC')
    analytics.repository.publish_files.assert_called_once_with(
        generation.publication, generation.durable, keep_previous=True, timezone='UTC')
    analytics.publish_file.assert_not_called()
    analytics.read_file.assert_not_called()


def test_partial_file_failure_keeps_sql_publication_untouched_and_can_be_retried(analytics):
    chunks, manifest, edition = bundle()
    analytics.publish_file.side_effect = [('first', 'digest', 10), OSError('volume unavailable')]
    with pytest.raises(OSError, match='volume unavailable'):
        analytics.prepare_generation(chunks, manifest, edition, 123, input_revision=17)
    analytics.repository.publish_files.assert_not_called()
    analytics.repository.publish_legacy.assert_not_called()
    analytics.publish_file.side_effect = None
    analytics.publish_file.return_value = ('file', 'digest', 10)
    generation = analytics.prepare_generation(chunks, manifest, edition, 123, input_revision=17)
    analytics.publish_generation(generation, 'UTC')
    analytics.repository.publish_files.assert_called_once()


def test_invalid_generation_is_rejected_before_any_file_is_written(analytics):
    chunks, manifest, edition = bundle()
    with pytest.raises(ValueError, match='missing selected listening chunk'):
        analytics.prepare_generation({}, manifest, edition, 123, input_revision=17)
    analytics.publish_file.assert_not_called()
    analytics.repository.publish_files.assert_not_called()


def test_legacy_generation_supplies_current_and_predecessor_chunk_retention(analytics):
    analytics.repository.uses_files.return_value = False
    analytics.repository.output.return_value = {'source': 'legacy', 'raw': '{"chunks":[{"key":"old"}]}'}
    chunks, manifest, edition = bundle()
    generation = analytics.prepare_generation(chunks, manifest, edition, 123, input_revision=17)
    analytics.publish_generation(generation, 'UTC')
    analytics.repository.publish_legacy.assert_called_once_with(
        (('listening_chunk:' + next(iter(chunks)), next(iter(chunks.values())), 123),
         ('listening_edition', edition, 123), ('listening_analytics', manifest, 123)),
        retained_surfaces=tuple(sorted(('listening_chunk:old', 'listening_chunk:' + next(iter(chunks))))),
        timezone='UTC')
    analytics.publish_file.assert_not_called()


def test_empty_legacy_generation_explicitly_retires_chunks(analytics):
    analytics.repository.uses_files.return_value = False
    analytics.repository.output.return_value = {'source': 'legacy', 'raw': None}
    generation = analytics.prepare_generation({}, '{}', '{}', 123, input_revision=17)
    analytics.publish_generation(generation, 'UTC')
    assert analytics.repository.publish_legacy.call_args.kwargs['retained_surfaces'] == ()


@pytest.mark.parametrize('surface', ['listening_analytics', 'listening_edition'])
def test_refreshing_one_native_document_retains_the_other_document_and_indexed_chunks(analytics, surface):
    chunks, manifest, edition = bundle()
    raw_by_surface = {'listening_analytics': manifest, 'listening_edition': edition,
                     **{'listening_chunk:' + key: raw for key, raw in chunks.items()}}
    analytics.repository.output.side_effect = lambda name, *args: {'source': 'legacy', 'raw': raw_by_surface[name]}
    analytics.publish(raw_by_surface[surface], 200, surface)
    publication, durable = analytics.repository.publish_files.call_args.args
    assert publication.memberships[0][1] == next(iter(chunks))
    assert {row[0] for row in durable} == {'listening_chunk', 'listening_analytics', 'listening_edition'}
    assert analytics.repository.publish_files.call_args.kwargs == {'keep_previous': True}


def test_native_chunks_require_a_complete_generation(analytics):
    with pytest.raises(ValueError, match='complete manifest'):
        analytics.publish('{}', 123, 'listening_chunk:' + 'a' * 24)
    analytics.publish_file.assert_not_called()
    analytics.repository.publish_files.assert_not_called()


def test_legacy_document_write_does_not_retire_unrelated_chunks(analytics):
    analytics.repository.uses_files.return_value = False
    analytics.publish('{"id":"new"}', 123, 'listening_edition')
    analytics.repository.publish_legacy.assert_called_once_with((('listening_edition', '{"id":"new"}', 123),))


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
