"""Listening-evidence controller ordering with mocked SQL and SQL-free files."""
import hashlib
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.evidence_ledger import EvidenceLedger
from yt_playlist.core import fao
from yt_playlist.core.evidence_ledger import EvidenceConflict, canonical
from yt_playlist.repos.evidence_files import EvidenceFileRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def ledger(tmp_path):
    repo = create_autospec(EvidenceFileRepo, instance=True, spec_set=True)
    selected = {}
    next_id = 256
    repo.references.side_effect = lambda keys=None: dict(selected) if keys is None else {key: selected[key] for key in keys if key in selected}
    def reserve(records):
        nonlocal next_id
        result = {}
        for key, integrity in records.items():
            next_id += 1
            result[key] = next_id, *integrity
        return result
    repo.reserve.side_effect = reserve
    repo.complete.side_effect = lambda records, previous: selected.update(records)
    return EvidenceLedger(repo, tmp_path, 1024 * 1024)


def test_reserve_precedes_file_and_completion_and_replay_preserves_identity(ledger, monkeypatch):
    events = []
    reserve = ledger.repository.reserve.side_effect
    complete = ledger.repository.complete.side_effect
    prepare = fao.prepare
    ledger.repository.reserve.side_effect = lambda records: events.append('reserve') or reserve(records)
    ledger.repository.complete.side_effect = lambda records, previous: events.append('complete') or complete(records, previous)
    def publish(*args, **kwargs):
        events.append('file')
        return prepare(*args, **kwargs)
    monkeypatch.setattr(fao, 'prepare', publish)
    value = {'id': '../../external', 'value': 'é'}
    assert ledger.put('response', value['id'], value)
    assert events == ['reserve', 'file', 'complete']
    path = ledger._path('response', value['id'])
    assert path == ledger.directory / 'response/01/257.json'
    assert ledger.get('response', value['id']) == value
    assert not ledger.put('response', value['id'], value)
    assert ledger._path('response', value['id']) == path
    ledger.repository.reserve.assert_called_once()
    with pytest.raises(EvidenceConflict):
        ledger.put('response', value['id'], {**value, 'value': 'changed'})


def test_file_failure_leaves_its_pending_identity_unselected(ledger, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError('interrupted publication')
    monkeypatch.setattr(fao, 'prepare', failed)
    with pytest.raises(OSError, match='interrupted publication'):
        ledger.put('response', 'new', {'id': 'new'})
    ledger.repository.reserve.assert_called_once()
    ledger.repository.complete.assert_not_called()
    assert ledger.get('response', 'new') is None
    assert ledger.records('response') == []


def test_completed_file_is_hidden_when_sql_completion_fails(ledger):
    ledger.repository.complete.side_effect = ValueError('head interrupted')
    with pytest.raises(ValueError, match='head interrupted'):
        ledger.put('response', 'new', {'id': 'new'})
    assert (ledger.directory / 'response/01/257.json').is_file()
    assert ledger.get('response', 'new') is None
    assert ledger.records('response') == []


def test_inline_context_and_semantic_artifact_keys_use_reserved_file_ids(ledger):
    context = {'saved': {'values': [0.4, 0.7]}}
    identity = hashlib.sha256(canonical(context)).hexdigest()
    offer = {'id': 'menu', 'context': identity}
    assert ledger.put('opportunity', 'menu', offer, artifact=context)
    assert ledger.get('artifact', identity) == context
    assert ledger.get('opportunity', 'menu') == offer
    assert ledger._path('artifact', identity).stem.isdecimal()
    assert ledger.artifact(context) == identity
    ledger.repository.reserve.assert_called_once()
