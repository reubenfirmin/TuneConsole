"""Policy meaning and file/SQL ordering without a database."""
from contextlib import contextmanager, nullcontext
import hashlib
import json
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.policies import Policies
from yt_playlist.core import policy_config
from yt_playlist.core.policy_files import PolicyFiles
from yt_playlist.repos.policies import PolicyRepo

pytestmark = pytest.mark.usefixtures('no_database')


def encoded(value):
    return json.dumps({'format': 'recommendation-policy-1', 'configuration': value},
                      sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def receipt(value, *, base=None):
    raw = encoded(value)
    return dict(configuration_filename='published', content_digest=hashlib.sha256(raw).hexdigest(),
                byte_size=len(raw), configuration_offset=0, created_at=10, base_version=base)


@pytest.fixture
def policies():
    repository = create_autospec(PolicyRepo, instance=True, spec_set=True)
    repository.file_owned.return_value = True
    repository.records.return_value = {'policies': {}, 'bodies': {}}
    repository.archived.return_value = None
    repository.unused_inventory.return_value = (0, 0)
    repository.legacy_root_messages.return_value = []
    files = create_autospec(PolicyFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    files.archived.return_value = None
    files.publish_many.side_effect = lambda values: [
        ('published', hashlib.sha256(encoded(value)).hexdigest(), len(encoded(value)), 0) for value in values]
    return Policies(repository, files, nullcontext)


@pytest.mark.parametrize('invalid', [
    ('other', '{"_policy_base":"absent"}', 10),
    ('other', '[]', 10), ('other', '{"value":NaN}', 10),
    ('other', '{}', float('nan')), ('', '{}', 10), ('other', '{"_policy_base":42}', 10),
])
def test_complete_batch_is_validated_before_any_file_or_sql_write(policies, invalid):
    with pytest.raises(ValueError):
        policies.publish([('valid', '{}', 10), invalid])
    policies.files.publish_many.assert_not_called()
    policies.repository.publish.assert_not_called()


def test_body_and_variant_are_published_before_their_sql_receipts(policies):
    events = []
    publish = policies.files.publish_many.side_effect
    policies.files.publish_many.side_effect = lambda values: (events.append('files'), publish(values))[1]
    policies.repository.publish.side_effect = lambda records: events.append('sql')
    policies.publish([('variant', '{"_policy_base":"base","ranker":"wheelhouse"}', 10)],
                     [('base', '{"algorithm":"rank-v1"}', 23, 10)])
    assert events == ['files', 'sql']
    records = policies.repository.publish.call_args.args[0]
    assert [(row['body'], row['version'], row['base']) for row in records] == [
        (True, 'base', None), (False, 'variant', 'base')]


def test_an_immutable_conflict_rejects_all_new_files(policies):
    policies.repository.records.return_value = {'policies': {'known': receipt({'value': 1})}, 'bodies': {}}
    with pytest.raises(ValueError, match='immutable'):
        policies.publish([('new', '{}', 10), ('known', '{"value":2}', 10)])
    policies.files.publish_many.assert_not_called()
    policies.repository.publish.assert_not_called()


def test_historical_policy_cannot_be_replaced_by_a_different_configuration(policies):
    policies.repository.archived.return_value = {'state': 'archived', 'publication': 'archive'}
    policies.files.archived.return_value = {'configuration': {'original': True}}
    with pytest.raises(ValueError, match='historical'):
        policies.publish([('archived', '{"original":false}', 10)])
    policies.files.publish_many.assert_not_called()


def test_budget_uses_wrapped_bytes_and_rejects_before_file_publication(policies, monkeypatch):
    value = {'algorithm': 'a'}
    raw = json.dumps(value)
    monkeypatch.setattr(policy_config, 'MAX_UNUSED_BYTES', len(encoded(value)) - 1)
    with pytest.raises(policy_config.PolicyCapacityFull):
        policies.publish([('new', raw, 10)], enforce_budget=True)
    policies.files.publish_many.assert_not_called()
    policies.repository.publish.assert_not_called()


@pytest.mark.parametrize('bad_receipts', [[], [('published', 'changed', 42, 0)]])
def test_incomplete_or_changed_file_publication_never_creates_a_sql_receipt(policies, bad_receipts):
    policies.files.publish_many.side_effect = None
    policies.files.publish_many.return_value = bad_receipts
    with pytest.raises(ValueError, match='publication changed'):
        policies.publish([('new', '{}', 10)])
    policies.repository.publish.assert_not_called()


def test_file_failure_never_starts_the_sql_write(policies):
    policies.files.publish_many.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        policies.publish([('new', '{}', 10)])
    policies.repository.publish.assert_not_called()


def test_existing_policy_repairs_impression_links_without_republishing_its_file(policies):
    policies.repository.records.return_value = {'policies': {'known': receipt({'value': 1})}, 'bodies': {}}
    policies.publish([('known', '{"value":1}', 20)])
    policies.files.publish_many.assert_called_once_with([])
    record, = policies.repository.publish.call_args.args[0]
    assert record['version'] == 'known'
    assert record['filename'] == 'published'


def test_configuration_composes_exact_shared_body_with_variant_overrides(policies):
    variant = {'_policy_base': 'base', 'ranker': 'variant'}
    body = {'ranker': 'base', 'weights': {'rock': 1.5}}
    policies.repository.records.side_effect = [
        {'policies': {'variant': receipt(variant, base='base')}, 'bodies': {}},
        {'policies': {}, 'bodies': {'base': receipt(body)}}]
    policies.files.read.side_effect = [variant, body]
    assert policies.configuration('variant') == {'ranker': 'variant', 'weights': {'rock': 1.5}}


def test_captured_policy_cannot_substitute_another_body(policies):
    policies.repository.records.side_effect = [
        {'policies': {'variant': receipt({}, base='original')}, 'bodies': {}},
        {'policies': {}, 'bodies': {}}]
    policies.files.read.return_value = {'_policy_base': 'different'}
    with pytest.raises(ValueError, match='shared-body reference'):
        policies.captured(['variant'])


def test_retirement_passes_explicit_cutoff_and_work_bounds_to_sql(policies, monkeypatch):
    monkeypatch.setattr(policy_config, 'SCAN_LIMIT', 7)
    monkeypatch.setattr(policy_config, 'SCAN_BYTES', 900)
    policies.repository.legacy_root_messages.return_value = ['{"tracks":[{"pool_policy":"held"}]}']
    policies.retention_candidates(100)
    policies.repository.retention_candidates.assert_called_once_with(
        100, protected={'held'}, limit=7, byte_limit=900, cursor_key=policy_config.CURSOR)


def test_retirement_rechecks_changed_legacy_roots_before_deletion(policies):
    policies.repository.legacy_root_messages.return_value = ['{"pool_policy":"newly-held"}']
    policies.prune(['unused', 'newly-held'], [1, 'newly-held'])
    policies.repository.prune.assert_called_once_with(['unused'], [1, 'newly-held'],
        cursor_key=policy_config.CURSOR, limit=policy_config.SCAN_LIMIT, byte_limit=policy_config.SCAN_BYTES)


def test_legacy_sql_configuration_does_not_open_a_file_owner(policies):
    policies.repository.file_owned.return_value = False
    policies.repository.legacy_capture.return_value = ([('legacy', '{"ranker":"old"}', 10)], [])
    assert policies.configuration('legacy') == {'ranker': 'old'}
    policies.files.retained.assert_not_called()


def test_history_rechecks_current_owners_then_publishes_files_before_sql(policies):
    events = []
    policies.repository.history_candidates.side_effect = lambda *args: events.append('candidates') or ['old']
    policies.repository.history_records.side_effect = lambda *args: events.append('current owners') or ([{'version': 'old'}], {})
    policies.repository.history_publication.return_value = 'previous'
    policies.files.archive_many.side_effect = lambda *args: events.append('archive file') or ('published', [('old', 1)])
    policies.repository.publish_history.side_effect = lambda *args: events.append('sql')
    assert policies.prepare_history(10000, limit=7) == 1
    assert events == ['candidates', 'current owners', 'archive file', 'sql']
    policies.repository.history_candidates.assert_called_once_with(6400, 7)
    policies.repository.history_records.assert_called_once_with(6400, ['old'])
    policies.repository.publish_history.assert_called_once_with('published', [('old', 1)])


def test_new_operational_owner_prevents_archiving_a_previous_candidate(policies):
    policies.repository.history_candidates.return_value = ['now-owned']
    policies.repository.history_records.return_value = ([], {})
    policies.files.archive_many.return_value = (None, [])
    assert policies.prepare_history(10000) == 0
    policies.repository.publish_history.assert_not_called()


def test_failed_history_file_publication_preserves_live_sql_owners(policies):
    policies.repository.history_candidates.return_value = ['old']
    policies.repository.history_records.return_value = ([{'version': 'old'}], {})
    policies.files.archive_many.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        policies.prepare_history(10000)
    policies.repository.publish_history.assert_not_called()


def test_history_expiry_commits_before_collection_and_releases_its_reader(policies):
    events = []

    @contextmanager
    def retained():
        events.append('retain')
        yield
        events.append('release')

    policies.files.retained.side_effect = retained
    policies.repository.history_available.return_value = True
    policies.repository.history_candidates.return_value = []
    policies.repository.expiring_history.return_value = ['first', 'next-batch']
    policies.repository.history_publication.side_effect = ['old', 'new']
    policies.files.expire_history.side_effect = lambda *args: events.append('file expiry') or ('new', ['first'])
    policies.repository.expire_history.side_effect = lambda *args: events.append('sql expiry')
    policies.files.collect_history.side_effect = lambda *args, **kwargs: events.append('collect')
    now = 200 * 86400
    assert policies.maintain_history(now) == 1
    assert events == ['retain', 'file expiry', 'sql expiry', 'release', 'collect']
    policies.repository.expiring_history.assert_called_once_with(20 * 86400, 128)
    policies.repository.expire_history.assert_called_once_with('new', ['first'])
    policies.files.collect_history.assert_called_once_with('new', 170 * 86400, limit=16)


def test_failed_history_expiry_does_not_collect_the_uncommitted_archive(policies):
    policies.repository.history_available.return_value = True
    policies.repository.history_candidates.return_value = []
    policies.repository.expiring_history.return_value = ['old']
    policies.files.expire_history.return_value = ('new', ['old'])
    policies.repository.expire_history.side_effect = RuntimeError('sql failure')
    with pytest.raises(RuntimeError, match='sql failure'):
        policies.maintain_history(200 * 86400)
    policies.files.collect_history.assert_not_called()
