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
    repository.records.return_value = {'policies': {}, 'bodies': {}}
    repository.archived.return_value = None
    repository.unused_inventory.return_value = (0, 0)
    repository.reserve_history.side_effect = lambda count: range(1, count + 1)
    files = create_autospec(PolicyFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    files.archived.return_value = None
    files.archive_records.return_value = []
    files.write_archive_reserved.side_effect = lambda records: nullcontext()
    files.history_garbage.side_effect = lambda *args, **kwargs: nullcontext(iter(()))
    files.publish.side_effect = lambda records: [
        ('/policies/pack.json', record['digest'], record['size'], 42) for record in records]
    return Policies(repository, files, nullcontext)


@pytest.mark.parametrize('invalid', [
    ('other', '{"_policy_base":"absent"}', 10),
    ('other', '[]', 10), ('other', '{"value":NaN}', 10),
    ('other', '{}', float('nan')), ('', '{}', 10), ('other', '{"_policy_base":42}', 10),
])
def test_complete_batch_is_validated_before_any_file_or_sql_write(policies, invalid):
    with pytest.raises(ValueError):
        policies.publish([('valid', '{}', 10), invalid])
    policies.files.publish.assert_not_called()
    policies.repository.publish.assert_not_called()


def test_body_and_variant_publish_files_before_sql_under_the_retained_reader(policies):
    events = []
    publish = policies.files.publish.side_effect

    @contextmanager
    def retained():
        events.append('retain')
        yield
        events.append('release')

    policies.files.retained.side_effect = retained
    policies.files.publish.side_effect = lambda records: (events.append('files'), publish(records))[1]
    policies.repository.publish.side_effect = lambda records: events.append('sql')
    policies.publish([('variant', '{"_policy_base":"base","ranker":"wheelhouse"}', 10)],
                     [('base', '{"algorithm":"rank-v1"}', 23, 10)])
    assert events == ['retain', 'files', 'sql', 'release']
    records = policies.repository.publish.call_args.args[0]
    assert [(row['body'], row['version'], row['base']) for row in records] == [
        (True, 'base', None), (False, 'variant', 'base')]


def test_an_immutable_conflict_rejects_all_new_files(policies):
    policies.repository.records.return_value = {'policies': {'known': receipt({'value': 1})}, 'bodies': {}}
    with pytest.raises(ValueError, match='immutable'):
        policies.publish([('new', '{}', 10), ('known', '{"value":2}', 10)])
    policies.files.publish.assert_not_called()
    policies.repository.publish.assert_not_called()


def test_historical_policy_cannot_be_replaced_by_a_different_configuration(policies):
    policies.repository.archived.return_value = {'state': 'archived', 'publication': 'archive'}
    policies.files.archived.return_value = {'configuration': {'original': True}}
    with pytest.raises(ValueError, match='historical'):
        policies.publish([('archived', '{"original":false}', 10)])
    policies.files.publish.assert_not_called()


def test_budget_uses_wrapped_bytes_and_rejects_before_file_publication(policies, monkeypatch):
    value = {'algorithm': 'a'}
    raw = json.dumps(value)
    monkeypatch.setattr(policy_config, 'MAX_UNUSED_BYTES', len(encoded(value)) - 1)
    with pytest.raises(policy_config.PolicyCapacityFull):
        policies.publish([('new', raw, 10)], enforce_budget=True)
    policies.files.publish.assert_not_called()
    policies.repository.publish.assert_not_called()


@pytest.mark.parametrize('bad_receipts', [[], [('published', 'changed', 42, 0)]])
def test_incomplete_or_changed_file_publication_never_creates_a_sql_receipt(policies, bad_receipts):
    policies.files.publish.return_value = bad_receipts
    policies.files.publish.side_effect = None
    with pytest.raises(ValueError, match='publication changed'):
        policies.publish([('new', '{}', 10)])
    policies.repository.publish.assert_not_called()


def test_file_failure_leaves_sql_owners_unchanged(policies):
    policies.files.publish.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        policies.publish([('new', '{}', 10)])
    policies.repository.publish.assert_not_called()


def test_existing_policy_repairs_impression_links_without_republishing_its_file(policies):
    policies.repository.records.return_value = {'policies': {'known': receipt({'value': 1})}, 'bodies': {}}
    policies.publish([('known', '{"value":1}', 20)])
    policies.files.publish.assert_called_once_with([])
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
    policies.retention_candidates(100)
    policies.repository.retention_candidates.assert_called_once_with(
        100, limit=7, byte_limit=900)


def test_retirement_passes_selected_versions_to_sql_for_recheck(policies):
    policies.prune(['unused', 'newly-held'])
    policies.repository.prune.assert_called_once_with(['unused', 'newly-held'],
        limit=policy_config.SCAN_LIMIT, byte_limit=policy_config.SCAN_BYTES)


def test_history_rechecks_current_owners_and_reserves_its_file_before_publication(policies):
    events = []
    policies.repository.history_candidates.side_effect = lambda *args: events.append('candidates') or ['old']
    policies.repository.history_records.side_effect = lambda *args: events.append('current owners') or ([{'version': 'old'}], {})
    records = [dict(version='old', created=1)]
    policies.files.archive_records.side_effect = lambda *args: events.append('capture') or records
    policies.repository.reserve_history.side_effect = lambda *args: events.append('reserve') or [7]

    @contextmanager
    def write(values):
        assert values == [records[0] | {'file_id': 7}]
        events.append('archive file')
        yield
        events.append('release')

    policies.files.write_archive_reserved.side_effect = write
    policies.repository.publish_history.side_effect = lambda *args: events.append('sql') or 1
    assert policies.prepare_history(10000, limit=7) == 1
    assert events == ['candidates', 'current owners', 'capture', 'reserve', 'archive file', 'sql', 'release']
    policies.repository.history_candidates.assert_called_once_with(6400, 7)
    policies.repository.history_records.assert_called_once_with(6400, ['old'])
    policies.repository.reserve_history.assert_called_once_with(1)
    policies.repository.publish_history.assert_called_once_with([records[0] | {'file_id': 7}], 6400)


def test_new_operational_owner_prevents_archiving_a_previous_candidate(policies):
    policies.repository.history_candidates.return_value = ['now-owned']
    policies.repository.history_records.return_value = ([], {})
    assert policies.prepare_history(10000) == 0
    policies.repository.publish_history.assert_not_called()
    policies.repository.reserve_history.assert_not_called()


def test_failed_history_file_publication_preserves_live_sql_owners(policies):
    policies.repository.history_candidates.return_value = ['old']
    policies.repository.history_records.return_value = ([{'version': 'old'}], {})
    policies.files.archive_records.return_value = [dict(version='old', created=1)]
    policies.files.write_archive_reserved.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        policies.prepare_history(10000)
    policies.repository.publish_history.assert_not_called()
    policies.repository.reserve_history.assert_called_once()


def test_history_expiry_commits_before_collection_and_retains_required_files(policies):
    events = []

    @contextmanager
    def garbage(*args, **kwargs):
        events.append('collect')
        yield iter([('required', 'info', 7), ('abandoned', 'info', 8), ('legacy', 'info', None)])
        events.append('release')

    policies.files.history_garbage.side_effect = garbage
    policies.repository.history_candidates.return_value = []
    policies.repository.expiring_history.return_value = ['first', 'next-batch']
    policies.repository.history_publication.return_value = 'previous'
    policies.repository.history_file_required.side_effect = [True, False]
    policies.repository.expire_history.side_effect = lambda *args: events.append('sql expiry')
    now = 200 * 86400
    assert policies.maintain_history(now) == 2
    assert events == ['sql expiry', 'collect', 'release']
    policies.repository.expiring_history.assert_called_once_with(20 * 86400, 128)
    policies.repository.expire_history.assert_called_once_with(['first', 'next-batch'])
    policies.files.history_garbage.assert_called_once_with('previous', 170 * 86400, limit=16)
    assert [call.args for call in policies.files.remove_history.call_args_list] == [('abandoned', 'info'), ('legacy', 'info')]


def test_failed_history_expiry_does_not_collect_the_uncommitted_archive(policies):
    policies.repository.history_candidates.return_value = []
    policies.repository.expiring_history.return_value = ['old']
    policies.repository.expire_history.side_effect = RuntimeError('sql failure')
    with pytest.raises(RuntimeError, match='sql failure'):
        policies.maintain_history(200 * 86400)
    policies.files.history_garbage.assert_not_called()
