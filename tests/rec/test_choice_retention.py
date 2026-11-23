"""Correction and chronology guarantees after releasing heavy diagnostic inputs."""
from tests.file_content_support import age_file
import json
from copy import deepcopy
from contextlib import closing, contextmanager
from functools import wraps
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.rec.test_choice_bandit import modes
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, native_sql_calls,
)
from yt_playlist.core import fao, schema_inspection
from yt_playlist.core.evidence_record_files import EvidenceRecordFiles
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence as choices, choice_retention
from yt_playlist.rec.choice_context import checkpoint, learning_context
from yt_playlist.library.choice_composition import resolve_context

DAY = 86400


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Retention owns SQL and file lifetimes without HTTP startup."""


def captured_groups(store):
    return [{**mode, 'centroid': mode['centroid'].tolist()}
            for mode in store.modes.list_modes(active_only=True)]


def captured(store, oid='menu', at=100, *, groups=None, format='mixes', diagnostic_repeat=40_000):
    groups = captured_groups(store) if groups is None else deepcopy(groups)
    oid = choices.issue(store, format, [{'id': 'one', 'genres':['Jazz'], 'score': 1},
                                        {'id': 'two', 'genres':['House'], 'score': 0}], at,
                        context={'at': at, 'groups': groups, 'layers': {'saved': oid * diagnostic_repeat}},
                        opportunity_id=oid)
    choices.respond(store, oid, 'visible', at + 1, visible=['one', 'two'])
    choices.respond(store, oid, 'choice', at + 2, option_id='one', action_id=oid + ':pick')
    return store.rec_ledger.evidence().get('opportunity', oid)


def age_artifacts(store):
    totals = {'checked': 0, 'aged': 0, 'logical_bytes': 0, 'verified_bytes': 0}
    for path in store.rec_ledger.evidence().directory.glob('artifact/*/*.json'):
        before = fao.read(path).info
        totals['checked'] += 1
        totals['logical_bytes'] += before.logical_bytes
        if before.mtime_ns == 10**9:
            continue
        raw = fao.read(path).bytes
        age_file(path, mtime_ns=10 ** 9)
        after = fao.read(path).info
        assert after.mtime_ns == 10**9 and after.ctime_ns == before.ctime_ns
        assert after.logical_bytes == before.logical_bytes == len(raw)
        assert fao.read(path).bytes == raw
        totals['aged'] += 1
        totals['verified_bytes'] += len(raw)
    return totals


@pytest.fixture
def retention_costs(monkeypatch, record_property):
    totals = {}

    def timed(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return operation(*args, **kwargs)
            finally:
                values = totals.setdefault(name, [0, 0.0, 0.0])
                values[0] += 1
                values[1] += time.perf_counter() - wall
                values[2] += time.thread_time() - cpu
        return call

    for owner, name in ((choices, 'issue'), (choices, 'respond'), (choices, 'maintain_storage'),
                        (fao, '_write'), (fao.Content, '__enter__')):
        monkeypatch.setattr(owner, name, timed(name, getattr(owner, name)))
    yield
    for name, (calls, wall, cpu) in sorted(totals.items()):
        record_property(name + '_calls', calls)
        record_property(name + '_wall_s', wall)
        record_property(name + '_thread_cpu_s', cpu)


@pytest.fixture
def restarting_library(tmp_path, monkeypatch, native_sql_calls):
    # Retention consumes the current schema. Admission is covered separately
    # in tests/core/test_current_schema.py.
    # Keep actual startup repair, repository binding and private revision guards
    # when reopening the persisted learning and diagnostic files below.
    from yt_playlist.core import migrations, sqlite_database as store_module

    entries = (('jazz', 'Jazz'), ('house', 'House'))
    bound_catalog(monkeypatch, entries=entries)
    with bounded_consumer_schema(entries) as schema:
        with _file_consumer_store(tmp_path, schema) as store:
            monkeypatch.setattr(migrations, 'migrate', lambda *args, **kwargs: False)
            # Reinstall the genuine trigger SQL in the reopened connection's
            # private TEMP namespace, as for the initial consumer connection.
            monkeypatch.setattr(store_module, 'Revisions', schema.bind_revisions)
            # Reader construction/admission has independent db_reads owners.
            # Reuse a real read-only connection while retaining actual file
            # leases only during each report, so collection can run between
            # reports. The reopened Store creates its own fresh readers.
            reader = store._make_reader()

            @contextmanager
            def read_view(**kwargs):
                with store._retained_readers():
                    assert not reader.conn.in_transaction
                    yield reader
                    assert not reader.conn.in_transaction

            monkeypatch.setattr(store, 'read_view', read_view)
            with closing(reader.conn), schema_inspection.reuse_tables(reader.conn):
                yield store


def test_compaction_preserves_learning_predictions_withdrawals_and_restart(restarting_library):
    store = restarting_library
    path = store._db_path
    modes(store)
    original = captured(store)
    before = choices.compile_report(store, 103)
    evaluated = choices.replay(store, 103)
    age_artifacts(store)
    result = choices.maintain_storage(store, 31 * DAY)
    ledger = store.rec_ledger.evidence()
    assert result['compacted_contexts'] == 1
    assert checkpoint(ledger, original['context'])['watermark'] == before['watermark']
    assert ledger.get('artifact', original['context']) is None
    assert ledger.get('opportunity', original['id']) == original
    assert learning_context(store, original['context']) == {'at':100,'precision':'theme-raw-genres-v1'}
    assert choices.compile_report(store, 103)['bandit'] == before['bandit']
    assert choices.replay(store, 103) == evaluated
    store.conn.close()
    store = Store(path)
    store.conn.execute('PRAGMA synchronous=OFF')
    with schema_inspection.reuse_tables(store.conn):
        store.init_schema()
    try:
        assert choices.compile_report(store, 103)['bandit'] == before['bandit']
        choices.respond(store, 'menu', 'withdraw', 32 * DAY, details={'action_id': 'menu:pick'})
        assert choices.compile_report(store, 32 * DAY)['formats']['mixes']['choices'] == 0
        assert choices.compile_report(store, 103)['formats']['mixes']['choices'] == 1
        assert not choices.respond(store, 'menu', 'choice', 33 * DAY, option_id='one', action_id='menu:pick')
        class Candidate:
            def predict(self, *args):
                pytest.fail('A released diagnostic context was passed to a custom predictor')
            def observe(self, event):
                pass
        replay = choices.replay(store, 103, Candidate())['formats']['mixes']
        assert replay['unsupported_precision'] == 1 and replay['scored'] == replay['future_context'] == 0
    finally:
        store.conn.close()


def test_failed_learning_publication_never_acknowledges_or_releases(store, monkeypatch):
    modes(store)
    offer = captured(store)
    age_artifacts(store)
    def fail(*args, **kwargs):
        raise OSError('publication failed')
    monkeypatch.setattr(store.choice_findings, 'publish', fail)
    with pytest.raises(OSError, match='publication failed'):
        choices.maintain_storage(store, 31 * DAY)
    ledger = store.rec_ledger.evidence()
    assert checkpoint(ledger, offer['context']) is None
    assert ledger.get('artifact', offer['context'])


def test_warm_retention_reuses_verified_offers_and_checkpoints(store, monkeypatch):
    offer = captured(store)
    assert not choices.maintain_storage(store, 200)['deferred']
    ledger = store.rec_ledger.evidence()
    saved = checkpoint(ledger, offer['context'])
    assert saved
    cached_paths = {ledger._path('opportunity', offer['id']), ledger._path('checkpoint', offer['context'])}
    read = EvidenceRecordFiles._read_bytes

    def cache_required(files, path):
        assert path not in cached_paths
        return read(files, path)

    with monkeypatch.context() as patch:
        patch.setattr(EvidenceRecordFiles, '_read_bytes', cache_required)
        assert not choices.maintain_storage(store, 200)['deferred']

    new = captured(store, oid='new-menu', at=201)
    assert checkpoint(ledger, new['context']) is None
    assert not choices.maintain_storage(store, 204)['deferred']
    assert checkpoint(ledger, new['context'])
    assert checkpoint(ledger, offer['context']) == saved


@pytest.fixture
def retention_trial(store, monkeypatch):
    modes(store)
    groups = captured_groups(store)
    # The compaction and failed-publication tests above own real learning
    # publication. This case owns repeated SQL/file retention under its byte cap.
    publish = Mock(return_value={'watermark': 'acknowledged-learning-generation'})
    from yt_playlist.library import choice_composition
    monkeypatch.setattr(choice_composition, 'reports', lambda database:
        SimpleNamespace(compile=lambda now: publish(database, now)))
    # Exercise the byte limit during a trial, well before the 30-day age limit.
    monkeypatch.setattr(choice_retention, 'DIAGNOSTIC_FRACTION', .003)
    return groups, publish


def test_optional_context_bytes_are_bounded_during_active_trial(
        store, retention_trial, retention_costs, record_property):
    groups, publish = retention_trial
    ledger = store.rec_ledger.evidence()
    snapshots, aging_seconds, aged, verified_bytes = [], 0.0, 0, 0
    # Initial, repeated and continuing compaction each exceed the real byte
    # cap. More identical days do not add a retention or chronology boundary.
    trial_days = 3
    for index in range(trial_days):
        # Mix envelopes now discard model diagnostics before capture. Album
        # contexts still retain the heavy graph whose byte budget this owns.
        offer = captured(store, oid=f'menu-{index}', at=(index + 1) * DAY,
                         groups=groups, format='albums', diagnostic_repeat=20_000)
        snapshots.append(offer['context'])
        diagnostic = ledger.get('artifact', offer['context'])['layers']['saved']['$artifact']
        assert ledger.get('artifact', diagnostic)
        started = time.perf_counter()
        aging = age_artifacts(store)
        aging_seconds += time.perf_counter() - started
        aged += aging['aged']
        verified_bytes += aging['verified_bytes']
        assert aging['logical_bytes'] > ledger.byte_budget * .003
        result = choices.maintain_storage(store, (index + 1) * DAY + 1000)
        publish.assert_called_with(store, (index + 1) * DAY + 1000)
        assert publish.call_count == index + 1
        assert result['optional_context_bytes'] <= ledger.byte_budget * .003
        assert len(ledger.records('checkpoint')) == index + 1
        assert checkpoint(ledger, offer['context'])['watermark'] == publish.return_value['watermark']
        assert ledger.get('artifact', diagnostic) is None
    assert len(ledger.records('opportunity')) == trial_days
    assert len(set(snapshots)) == trial_days
    assert captured_groups(store) == groups
    # Only compact manifests may remain inside the optional budget.
    assert sum(len(json.dumps(a)) for a in ledger.records('artifact')) < ledger.byte_budget * .003
    assert all(checkpoint(ledger, identity) for identity in snapshots)
    record_property('trial_aging_seconds', aging_seconds)
    record_property('trial_aged_artifacts', aged)
    record_property('trial_verified_artifact_bytes', verified_bytes)


def test_prepared_context_keeps_its_graph_and_prior_checkpoint_after_retention(store, retention_trial):
    groups, publish = retention_trial
    ledger = store.rec_ledger.evidence()
    previous = captured(store, oid='menu-0', at=DAY, groups=groups,
                        format='albums', diagnostic_repeat=20_000)
    age_artifacts(store)
    choices.maintain_storage(store, DAY + 1000)
    assert checkpoint(ledger, previous['context'])['watermark'] == publish.return_value['watermark']
    assert ledger.get('artifact', previous['context']) is None

    held_at = 4 * DAY
    held = captured(store, oid='held', at=held_at, groups=groups,
                    format='albums', diagnostic_repeat=20_000)
    held_context = resolve_context(store, {'artifact': held['context']})
    assert held_context['layers']['saved'] == 'held' * 20_000
    store.rec_proposals.put_proposals('home_prepared', {'choice_context': {'artifact': held['context']}}, held_at)
    age_artifacts(store)
    choices.maintain_storage(store, 100 * DAY)
    assert resolve_context(store, {'artifact': held['context']}) == held_context
    assert checkpoint(ledger, previous['context'])
