"""Real unlink and reconciliation cutoffs must finish on a fresh ledger."""
from tests.file_content_support import age_file
from contextlib import closing, contextmanager
import shutil
import sqlite3

import pytest

from yt_playlist.core import fao
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.evidence_ledger import EvidenceLedger
from yt_playlist.rec.choice_capture import CaptureQueue


def menu(ledger, identity, *, response=None):
    context = ledger.artifact({'snapshot': identity})
    ledger.put('opportunity', identity, {'id': identity, 'context': context, 'observed_at': 1})
    for index in range(8):
        event = f'{identity}-visible-{index}'
        ledger.put('response', event, {'id': event, 'opportunity_id': identity,
                                      'kind': 'visible', 'observed_at': 2})
    if response:
        ledger.put('response', identity, {'id': identity, 'opportunity_id': identity,
                                         'kind': response, 'observed_at': 3})
    ledger.put('checkpoint', context, {'source_context': context, 'learning': identity})
    age_file(ledger._path('artifact', context), mtime_ns=10 ** 9)
    return context


def exact_bytes(ledger, result):
    used = sum(fao.read(path).info.logical_bytes for path in ledger.directory.glob('*/*/*.json'))
    assert result['used_bytes'] == int((ledger.directory / '.budget').read_text()) == used
    assert not result['deferred']


def native_image(directory):
    # Physical bytes include FAO's logical clocks/digest trailer. Keep the
    # budget, event revision and lock files too; copy2 retains filesystem mtime.
    return {path.relative_to(directory): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in directory.rglob('*') if path.is_file()}


@pytest.fixture(scope='module')
def ledger_prefix(tmp_path_factory):
    root = tmp_path_factory.mktemp('unlink-ledger-prefixes')
    prefixes = {}

    def source(shape):
        assert shape in {'expired', 'retention', 'capture'}
        if shape not in prefixes:
            directory = root / shape
            if shape == 'expired':
                ledger = EvidenceLedger(directory, 1_000_000)
                contexts = {'expired': menu(ledger, 'expired')}
            else:
                ledger, contexts = restore('expired', directory)
                additions = (('chosen', 'choice'), ('corrected', 'withdraw')) if shape == 'retention' else (
                    ('choice', None), ('pending', None))
                for identity, response in additions:
                    contexts[identity] = menu(ledger, identity, response=response)
            # All real publications and age changes have finished. Retain only
            # immutable metadata and files, with no active ledger/lock handle.
            prefixes[shape] = (directory, tuple(contexts.items()), tuple(native_image(directory).items()))
        return prefixes[shape]

    def restore(shape, destination):
        directory, contexts, image = source(shape)
        assert native_image(directory) == dict(image), 'Ledger prefix was modified'
        assert not destination.exists() or not any(destination.iterdir())
        shutil.copytree(directory, destination, dirs_exist_ok=True)
        assert native_image(destination) == dict(image)
        assert all(not (directory / relative).samefile(destination / relative) for relative, _ in image)
        # Every file, including the accounting/lock inode, is a private copy.
        # EvidenceLedger acquires new destination-local locks on each operation.
        return EvidenceLedger(destination, 1_000_000), dict(contexts)

    yield restore
    for directory, _, image in prefixes.values():
        assert native_image(directory) == dict(image), 'Ledger prefix was modified'


@pytest.mark.parametrize('boundary', ['opportunity', 'response', 'checkpoint', 'artifact'])
def test_restart_after_real_unlink_finishes_events_and_reconciles_ids(tmp_path, monkeypatch, boundary, ledger_prefix):
    ledger, contexts = ledger_prefix('retention', tmp_path)
    context = contexts['expired']
    retained = {path: fao.read(path).bytes for path in tmp_path.glob('*/*/*.json')
                if path not in {ledger._path('opportunity', 'expired'), ledger._path('artifact', context),
                                ledger._path('checkpoint', context)}
                and path not in {ledger._path('response', f'expired-visible-{i}') for i in range(8)}}
    delete, interrupted, callbacks = fao.delete, [], []

    def cutoff(path, **kwargs):
        removed = delete(path, **kwargs)
        if removed and path.parts[-3] == boundary and not interrupted:
            interrupted.append(path)
            raise WorkBudgetExceeded('Slice ended immediately after a real unlink')
        return removed

    monkeypatch.setattr(fao, 'delete', cutoff)
    with pytest.raises(WorkBudgetExceeded):
        ledger.maintain(10, retire_references=lambda offers, checkpoints: callbacks.append((offers, checkpoints)))
    assert interrupted and not callbacks
    monkeypatch.setattr(fao, 'delete', delete)
    restarted = EvidenceLedger(tmp_path, 1_000_000)
    result = restarted.maintain(10, retire_references=lambda offers, checkpoints: callbacks.append((offers, checkpoints)))
    assert callbacks == [(['expired'], [context])]
    assert restarted.get('opportunity', 'expired') is None
    assert all(event['opportunity_id'] != 'expired' for event in restarted.records('response'))
    assert restarted.get('checkpoint', context) is None
    assert restarted.get('artifact', context) is None
    assert all(fao.read(path).bytes == value for path, value in retained.items())
    exact_bytes(restarted, result)


def test_restart_rechecks_choices_and_current_capture_pins_before_replaying_intent(tmp_path, monkeypatch, ledger_prefix):
    ledger, contexts = ledger_prefix('capture', tmp_path)
    delete = fao.delete

    def cutoff(*args, **kwargs):
        raise WorkBudgetExceeded('Intent published; no unlink yet')

    monkeypatch.setattr(fao, 'delete', cutoff)
    with pytest.raises(WorkBudgetExceeded):
        ledger.maintain(10)
    monkeypatch.setattr(fao, 'delete', delete)
    ledger.put('response', 'new-choice', {'id': 'new-choice', 'opportunity_id': 'choice',
                                         'kind': 'choice', 'observed_at': 3})
    captures = CaptureQueue(lambda capture: True)
    assert captures.enqueue('response', lambda: None, references=('pending',))
    new_root = ledger.artifact({'newly': 'prepared'})
    age_file(ledger._path('artifact', new_root), mtime_ns=10 ** 9)
    assert captures.enqueue('menu', lambda: None, artifacts=(new_root,))
    callbacks = []
    restarted = EvidenceLedger(tmp_path, 1_000_000)
    result = restarted.maintain(10, pending_protection=captures.protection,
        retire_references=lambda offers, checkpoints: callbacks.append((offers, checkpoints)))
    assert callbacks == [(['expired'], [contexts['expired']])]
    for identity in ('choice', 'pending'):
        assert restarted.get('opportunity', identity)
        assert restarted.get('artifact', contexts[identity])
        assert restarted.get('checkpoint', contexts[identity])
    assert restarted.get('response', 'new-choice') and restarted.get('artifact', new_root)
    exact_bytes(restarted, result)


def test_blocked_unlink_stays_pending_and_does_not_release_offer_references(tmp_path, monkeypatch, ledger_prefix):
    ledger, _ = ledger_prefix('expired', tmp_path)
    blocked = ledger._path('response', 'expired-visible-0')
    delete, callbacks = fao.delete, []
    monkeypatch.setattr(fao, 'delete', lambda path, **kwargs: False if path == blocked else delete(path, **kwargs))
    result = ledger.maintain(10, retire_references=lambda offers, checkpoints: callbacks.extend(offers))
    assert result['deferred'] and result['retired_unanswered'] == 0
    assert blocked.exists() and not callbacks
    monkeypatch.setattr(fao, 'delete', delete)
    restarted = EvidenceLedger(tmp_path, 1_000_000)
    result = restarted.maintain(10, retire_references=lambda offers, checkpoints: callbacks.extend(offers))
    assert not blocked.exists() and callbacks == ['expired']
    exact_bytes(restarted, result)


def test_reference_callbacks_commit_prefixes_and_retry_only_unacknowledged_batch(tmp_path):
    ledger = EvidenceLedger(tmp_path, 1_000_000)
    context = ledger.artifact({'shared': 'old'})
    for index in range(70):
        identity = f'old-{index:03}'
        ledger.put('opportunity', identity, {'id': identity, 'context': context, 'observed_at': 1})
    attempted, committed = [], set()

    def cutoff(offers, checkpoints):
        attempted.append(offers)
        committed.update(offers)  # Model a successful idempotent SQL transaction.
        if len(attempted) == 2:
            raise WorkBudgetExceeded('Callback committed; acknowledgement not saved')

    with pytest.raises(WorkBudgetExceeded):
        ledger.maintain(10, retire_references=cutoff)
    assert len(attempted[0]) == len(attempted[1]) == 32
    repeated = []

    def finish(offers, checkpoints):
        repeated.extend(offers)
        committed.update(offers)

    restarted = EvidenceLedger(tmp_path, 1_000_000)
    result = restarted.maintain(10, retire_references=finish)
    assert len(committed) == 70
    assert set(repeated).isdisjoint(attempted[0])
    assert set(attempted[1]) <= set(repeated)
    exact_bytes(restarted, result)


def test_restart_repairs_accounting_if_slice_ends_before_budget_save(tmp_path, monkeypatch, ledger_prefix):
    ledger, _ = ledger_prefix('expired', tmp_path)
    budget = ledger._budget

    @contextmanager
    def cutoff():
        with budget() as (used, save):
            def interrupted(value):
                raise WorkBudgetExceeded('Unlinks finished; byte accounting not saved')
            yield used, interrupted

    monkeypatch.setattr(ledger, '_budget', cutoff)
    with pytest.raises(WorkBudgetExceeded):
        ledger.maintain(10)
    assert not list(tmp_path.glob('*/*/*.json'))
    restarted = EvidenceLedger(tmp_path, 1_000_000)
    exact_bytes(restarted, restarted.maintain(10))


def test_damaged_retirement_intent_is_not_silently_discarded(tmp_path, ledger_prefix):
    ledger, _ = ledger_prefix('expired', tmp_path)
    fao.write(tmp_path / '.retention-retirements', b'{"schema":1,"offers":false}')
    with pytest.raises(ValueError, match='retirement intent'):
        ledger.maintain(10)
    assert ledger.get('opportunity', 'expired')


def test_backup_after_partial_unlink_preserves_pending_retirement(tmp_path, monkeypatch, ledger_prefix):
    from yt_playlist.core import choice_evidence_files

    with closing(sqlite3.connect(tmp_path / 'source.db')) as source, \
            closing(sqlite3.connect(tmp_path / 'restored.db')) as restored:
        ledger, contexts = ledger_prefix('expired', choice_evidence_files.directory(source))
        context = contexts['expired']
        remove = fao.delete

        def cutoff(path, **kwargs):
            result = remove(path, **kwargs)
            if path == ledger._path('opportunity', 'expired'):
                raise WorkBudgetExceeded('Menu removed, events still present')
            return result

        monkeypatch.setattr(fao, 'delete', cutoff)
        with pytest.raises(WorkBudgetExceeded):
            ledger.maintain(10)
        monkeypatch.setattr(fao, 'delete', remove)
        manifest = choice_evidence_files.export(source, tmp_path / 'backup')
        choice_evidence_files.restore(restored, manifest)
        copied = EvidenceLedger(choice_evidence_files.directory(restored), 1_000_000)
        assert copied.get('opportunity', 'expired') is None
        assert copied.records('response')
        callbacks = []
        result = copied.maintain(10,
            retire_references=lambda offers, checkpoints: callbacks.append((offers, checkpoints)))
        assert callbacks == [(['expired'], [context])]
        assert not copied.records('response')
        exact_bytes(copied, result)


def test_private_prefix_copies_keep_bytes_accounting_and_locks_independent(tmp_path, ledger_prefix):
    left, left_contexts = ledger_prefix('expired', tmp_path / 'left')
    right, right_contexts = ledger_prefix('expired', tmp_path / 'right')
    assert left is not right and left_contexts == right_contexts
    before = native_image(left.directory)
    # A real publication on the right must complete while the left owns its
    # accounting lock. The copy check above rejects shared inodes before this.
    with (left.directory / '.budget').open('r+b') as stream:
        unlock = fao._try_file_lock(stream, exclusive=True)
        assert unlock is not None
        try:
            assert right.put('response', 'private-choice', {
                'id': 'private-choice', 'opportunity_id': 'expired', 'kind': 'choice', 'observed_at': 3})
        finally:
            unlock()
    assert right.get('response', 'private-choice')
    assert left.get('response', 'private-choice') is None
    assert native_image(left.directory) == before
    assert (right.directory / '.events').read_bytes() != (left.directory / '.events').read_bytes()
    with right._budget() as (used, _save):
        assert used == int((right.directory / '.budget').read_text()) == sum(
            fao.read(path).info.logical_bytes for path in right.directory.glob('*/*/*.json'))
