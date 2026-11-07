"""Model identity and retirement policy with mocked SQL and file owners."""
from contextlib import nullcontext
import hashlib
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.vibe_models import VibeModels
from yt_playlist.core import fao, vibe_model_files
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.vibe_provenance import ProvenanceFiles
from yt_playlist.repos.vibe_models import VibeModelsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def models():
    return VibeModels(create_autospec(VibeModelsRepo, instance=True, spec_set=True),
        create_autospec(vibe_model_files.VibeModelFiles, instance=True, spec_set=True),
        create_autospec(ProvenanceFiles, instance=True, spec_set=True))


def original(measurement=7, recording=3):
    return dict(measurement=measurement, recording=recording, score=.8, support=.6,
                source='provider/v', input_digest='digest', version='v')


def measured(item, **changes):
    return dict(id=item['measurement'], recording_id=item['recording'], vibe='relax',
        score=item['score'], support=item['support'], source='provider', input_digest='digest',
        exact_version='v', **changes)


@pytest.mark.parametrize('field', ['missing', 'recording_id', 'vibe', 'score', 'support',
                                  'source', 'input_digest', 'exact_version'])
def test_model_requires_exact_original_measurements(models, field):
    item = original()
    models.files.read.return_value = {'vibe': 'relax', 'inputs': [item]}
    row = measured(item)
    if field != 'missing':
        row[field] = 'changed'
    models.repository.measurements.return_value = [] if field == 'missing' else [row]
    with pytest.raises(fao.CorruptFile, match='original measurements'):
        models.read('generation')


def test_original_validation_uses_bounded_batches(models):
    items = [original(i, i) for i in range(1, 130)]
    value = {'vibe': 'relax', 'inputs': items}
    models.files.read.return_value = value
    models.repository.measurements.side_effect = [[measured(item) for item in items[:128]], [measured(items[-1])]]
    assert models.read('generation') is value
    assert [len(call.args[0]) for call in models.repository.measurements.call_args_list] == [128, 1]


def rebuilt(models):
    generation, item = 'a' * 64, original()
    models.files.read.return_value = {'vibe': 'relax', 'inputs': [item]}
    models.repository.measurements.return_value = [measured(item)]
    models.repository.receipt_facts.return_value = {
        'prediction': ('relax', 8), 'generation': None, 'marker': None, 'artists': {4}}
    receipt = vibe_model_files.receipt(generation, [4], .01, .1)
    publication = Mock()
    models.provenance.stage_many.return_value = [publication], [(9, b'fingerprint', 100)], []
    return receipt, publication


def test_rebuilt_receipt_files_precede_sql_references(models):
    receipt, publication = rebuilt(models)
    events = []
    publication.publish.side_effect = lambda: events.append('file')
    models.repository.install_receipts.side_effect = lambda *args: events.append('sql')
    models.publish_receipts([(9, receipt, [4])])
    assert events == ['file', 'sql']
    models.repository.install_receipts.assert_called_once_with(
        [(9, bytes.fromhex(receipt['generation']))], [7], [(9, b'fingerprint', 100)])
    publication.close.assert_called_once()


@pytest.mark.parametrize('failure', ['self', 'vibe', 'artist', 'artists', 'generation', 'marker', 'variance'])
def test_invalid_rebuilt_receipt_cannot_prepare_files_or_write_sql(models, failure):
    receipt, _publication = rebuilt(models)
    facts = models.repository.receipt_facts.return_value
    if failure == 'self':
        facts['prediction'] = ('relax', 3)
    elif failure == 'vibe':
        facts['prediction'] = ('focus', 8)
    elif failure == 'artist':
        facts['artists'] = set()
    elif failure == 'artists':
        receipt['artists'] = []
    elif failure == 'generation':
        facts['generation'] = 'b' * 64
    elif failure == 'marker':
        facts['marker'] = (b'different', 'ready', receipt['generation'])
    else:
        receipt['new_track_variance'] = 0
    with pytest.raises(ValueError):
        models.publish_receipts([(9, receipt, [4])])
    models.provenance.stage_many.assert_not_called()
    models.repository.install_receipts.assert_not_called()


def test_exact_rebuilt_receipt_is_idempotently_admitted(models):
    receipt, _publication = rebuilt(models)
    models.repository.receipt_facts.return_value.update(
        generation=receipt['generation'], marker=(hashlib.sha256(canonical(receipt)).digest(), 'ready', receipt['generation']))
    models.publish_receipts([(9, receipt, [4])])
    models.repository.install_receipts.assert_called_once()


def test_failed_receipt_file_publication_closes_files_without_sql(models):
    receipt, publication = rebuilt(models)
    publication.publish.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        models.publish_receipts([(9, receipt, [4])])
    publication.close.assert_called_once()
    models.repository.install_receipts.assert_not_called()


def test_retirement_preserves_references_and_recent_unpublished_files(models):
    work = create_autospec(vibe_model_files.ModelCollection, instance=True, spec_set=True)
    models.files.collection.side_effect = lambda: nullcontext(work)
    work.generations.return_value = iter(['referenced', 'recent', 'old'])
    models.repository.referenced.side_effect = [True, False, False]
    now = 100 * 86400
    work.created_at.side_effect = [now, now - 30 * 86400]
    assert models.collect(now, 16) == 1
    work.generations.assert_called_once_with(16)
    assert [call.args[0] for call in work.created_at.call_args_list] == ['recent', 'old']
    work.remove.assert_called_once_with('old')
    work.save.assert_called_once()


def test_active_reader_prevents_retirement_without_sql_reference_queries(models):
    models.files.collection.side_effect = lambda: nullcontext(None)
    assert models.collect(100 * 86400) == 0
    models.repository.referenced.assert_not_called()


@pytest.mark.parametrize('limit', [0, 129])
def test_invalid_collection_budget_does_not_enter_file_owner(models, limit):
    with pytest.raises(ValueError, match='bounded limit'):
        models.collect(0, limit)
    models.files.collection.assert_not_called()
