from concurrent.futures import ThreadPoolExecutor
import hashlib
import os

import pytest

from yt_playlist.core.evidence_ledger import (
    EvidenceBudgetExceeded, EvidenceConflict, EvidenceLedger, canonical, library_byte_budget,
)
from yt_playlist.core.store import Store


def test_retry_conflict_restart_and_safe_paths(tmp_path):
    ledger = EvidenceLedger(tmp_path, byte_budget=1024 * 1024)
    value = {"schema": 1, "at": 1, "choice": "A"}
    assert ledger.put("response", "../../outside", value)
    assert not EvidenceLedger(tmp_path, byte_budget=1024 * 1024).put("response", "../../outside", value)
    assert ledger.get("response", "../../outside") == value
    with pytest.raises(EvidenceConflict):
        ledger.put("response", "../../outside", {**value, "choice": "B"})
    assert len(list(tmp_path.rglob("*.json"))) == 1
    assert ledger.records("response") == [value]
    assert not list(tmp_path.rglob(".pending-*"))


def test_competing_publications_are_atomic_and_artifacts_are_shared(tmp_path):
    ledger = EvidenceLedger(tmp_path, byte_budget=1024 * 1024)
    value = {"options": ["A", "B"], "state": {"now": {}, "session": {}, "recent": {}, "saved": {}}}
    with ThreadPoolExecutor(4) as workers:
        published = list(workers.map(lambda _: ledger.put("opportunity", "same", value), range(12)))
    assert sum(published) == 1
    first = ledger.artifact(value)
    assert ledger.artifact(value) == first
    assert len(ledger.records("artifact")) == 1


def test_failed_publication_keeps_previous_evidence(tmp_path, monkeypatch):
    ledger = EvidenceLedger(tmp_path, byte_budget=1024 * 1024)
    ledger.put("response", "good", {"choice": "A"})

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("yt_playlist.core.evidence_ledger.os.link", fail)
    with pytest.raises(OSError, match="disk full"):
        ledger.put("response", "new", {"choice": "B"})
    assert ledger.get("response", "new") is None
    assert ledger.records("response") == [{"choice": "A"}]
    assert not list(tmp_path.rglob(".pending-*"))


def test_repository_evidence_survives_restart_and_reader_uses_same_ledger(tmp_path):
    path = tmp_path / "state.db"
    first = Store(path)
    first.init_schema()
    try:
        first.rec_ledger.evidence().put("opportunity", "one", {"id": "one"})
        with first.read_snapshot() as snapshot:
            assert snapshot.store.rec_ledger.evidence().get("opportunity", "one") == {"id": "one"}
    finally:
        first.conn.close()
    second = Store(path)
    second.init_schema()
    try:
        assert second.rec_ledger.evidence().get("opportunity", "one") == {"id": "one"}
    finally:
        second.conn.close()


def test_budget_is_shared_across_restarts_and_competing_writers(tmp_path):
    def write(index):
        try:
            return EvidenceLedger(tmp_path, byte_budget=100).put("response", index, {"data": "x" * 40})
        except EvidenceBudgetExceeded:
            return False
    with ThreadPoolExecutor(4) as workers:
        assert sum(workers.map(write, range(12))) == 1
    assert len(EvidenceLedger(tmp_path, byte_budget=100).records("response")) == 1
    with pytest.raises(EvidenceBudgetExceeded):
        EvidenceLedger(tmp_path, byte_budget=100).put("response", "more", {"data": "x" * 40})


def test_context_reservation_cannot_publish_a_dangling_menu_at_capacity(tmp_path):
    ledger = EvidenceLedger(tmp_path, byte_budget=40)
    with pytest.raises(EvidenceBudgetExceeded):
        ledger.put("opportunity", "menu", {"id": "menu"}, artifact={"large": "x" * 40})
    assert ledger.get("opportunity", "menu") is None
    assert ledger.records("artifact") == []


@pytest.mark.parametrize('existing_manifest', [False, True])
def test_capacity_error_reports_record_and_only_missing_artifact_bytes(tmp_path, existing_manifest):
    ledger = EvidenceLedger(tmp_path, byte_budget=100_000)
    component = ledger.artifact({'shared': 'x' * 1000})
    manifest = {'model': {'$artifact': component}}
    identity = hashlib.sha256(canonical(manifest)).hexdigest()
    if existing_manifest:
        assert ledger.artifact(manifest) == identity
    record = {'id': 'new', 'context': identity, 'observed_at': 100}
    required = len(canonical(record)) + (0 if existing_manifest else len(canonical(manifest)))
    used = sum(path.stat().st_size for path in tmp_path.glob('*/*/*.json'))
    ledger.byte_budget = used + required - 1
    with pytest.raises(EvidenceBudgetExceeded) as failure:
        ledger.put('opportunity', 'new', record, artifact=manifest)
    assert failure.value.required_bytes == required
    assert failure.value.used_bytes == used
    assert failure.value.budget_bytes == used + required - 1
    assert int((tmp_path / '.budget').read_text()) == used
    assert ledger.get('opportunity', 'new') is None
    assert ledger.get('artifact', component) == {'shared': 'x' * 1000}


def test_collection_between_manifest_read_and_offer_publication_abstains(tmp_path):
    ledger = EvidenceLedger(tmp_path, byte_budget=100_000)
    component = ledger.artifact({'saved': 'large context'})
    manifest = {'saved': {'$artifact': component}}
    ledger.maintain(10**12)
    with pytest.raises(ValueError, match='component is no longer retained'):
        ledger.put('opportunity', 'late', {'id': 'late'}, artifact=manifest)
    assert ledger.get('opportunity', 'late') is None


def test_reusing_an_immutable_prepared_manifest_renews_its_component_grace(tmp_path):
    import time
    ledger = EvidenceLedger(tmp_path, byte_budget=100_000)
    component = ledger.artifact({'saved': 'shared'})
    manifest = {'saved': {'$artifact': component}}
    identity = ledger.artifact(manifest)
    for path in tmp_path.glob('artifact/*/*.json'):
        os.utime(path, (1, 1))
    assert ledger.artifact(manifest) == identity
    ledger.maintain(10**12, orphan_before=time.time() - 60)
    assert ledger.get('artifact', identity) == manifest
    assert ledger.get('artifact', component) == {'saved': 'shared'}


def test_event_generation_survives_restarts_and_ignores_artifact_and_retry_writes(tmp_path):
    first_ledger = EvidenceLedger(tmp_path, byte_budget=1024 * 1024)
    initial = first_ledger.generation()
    first_ledger.put('response', 'event', {'kind': 'choice'})
    revision = first_ledger.generation()
    assert revision != initial
    second_ledger = EvidenceLedger(tmp_path, byte_budget=1024 * 1024)
    assert second_ledger.generation() == revision
    assert not second_ledger.put('response', 'event', {'kind': 'choice'})
    second_ledger.artifact({'model': 'new context'})
    second_ledger.put('report', 'daily', {'at': 100})
    assert first_ledger.generation() == revision
    second_ledger.put('response', 'correction', {'kind': 'withdraw'})
    assert first_ledger.generation() != revision


def test_generation_reader_waits_for_the_event_publication_boundary(tmp_path, monkeypatch):
    import threading
    first_ledger = EvidenceLedger(tmp_path, byte_budget=1024 * 1024)
    previous = first_ledger.generation()
    entered, release = threading.Event(), threading.Event()
    publish = first_ledger._publish

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return publish(*args)

    monkeypatch.setattr(first_ledger, '_publish', paused)
    second_ledger = EvidenceLedger(tmp_path, byte_budget=1024 * 1024)
    with ThreadPoolExecutor(2) as workers:
        write = workers.submit(first_ledger.put, 'response', 'event', {'kind': 'choice'})
        assert entered.wait(5)
        read = workers.submit(second_ledger.generation)
        try:
            assert not read.done()
        finally:
            release.set()
        assert write.result(5)
        assert read.result(5) != previous
    assert second_ledger.get('response', 'event') == {'kind': 'choice'}


def test_retiring_unanswered_events_advances_the_event_generation(tmp_path):
    ledger = EvidenceLedger(tmp_path, byte_budget=1024 * 1024)
    context = ledger.artifact({})
    ledger.put('opportunity', 'old', {'id': 'old', 'observed_at': 1, 'context': context})
    previous = ledger.generation()
    assert ledger.maintain(2)['retired_unanswered'] == 1
    assert ledger.generation() != previous


def test_orphan_grace_is_independent_of_choice_retention_and_protects_components(tmp_path):
    ledger = EvidenceLedger(tmp_path, byte_budget=1024 * 1024)
    component = ledger.artifact({'large': 'kept'})
    prepared = ledger.artifact({'model': {'$artifact': component}})
    orphan = ledger.artifact({'old': 'abandoned'})
    reused = ledger.artifact({'old': 'reused by in-flight capture'})
    in_flight = ledger.artifact({'new': {'$artifact': reused}})
    chosen = ledger.artifact({'chosen': 'durable'})
    ledger.put('opportunity', 'choice', {'id': 'choice', 'observed_at': 100, 'context': chosen})
    ledger.put('response', 'selected', {'id': 'selected', 'opportunity_id': 'choice',
                                       'kind': 'choice', 'observed_at': 101})
    for identity in (component, prepared, orphan, chosen, reused):
        os.utime(ledger._path('artifact', identity), (100, 100))
    os.utime(ledger._path('artifact', in_flight), (1000, 1000))
    result = ledger.maintain(50, protected_artifacts=[prepared], orphan_before=900)
    assert ledger.get('artifact', orphan) is None
    for identity in (component, prepared, in_flight, chosen, reused):
        assert ledger.get('artifact', identity) is not None
    assert ledger.get('response', 'selected') is not None
    assert result['used_bytes'] == sum(p.stat().st_size for p in tmp_path.glob('*/*/*.json'))


def test_capacity_tracks_library_growth_without_counting_discovery_or_duplicate_entries(store):
    # A stale setting from the former fixed cap cannot override automatic sizing.
    store.settings.set_setting('rec_param:choice_storage_mb', '16')
    initial = store.rec_ledger.evidence()
    assert initial.byte_budget == library_byte_budget(0)
    initial.put('response', 'kept', {'choice': 'A'})
    entry = store.tracks.upsert_track('saved', 'Song', 'Artist', None, 180)
    store.tracks.observe_track_entry('discovery', 'Other song', 'Other artist')
    with store.conn:
        duplicate = store.conn.execute(
            "INSERT INTO catalog_entries(recording_id,source_ref) "
            "SELECT recording_id,'second-source' FROM catalog_entries WHERE id=?", (entry,)).lastrowid
        store.conn.execute("INSERT INTO library_memberships VALUES (?,'second-source',1)", (duplicate,))
    grown = store.rec_ledger.evidence()
    assert grown.byte_budget == library_byte_budget(1)
    assert grown.get('response', 'kept') == {'choice': 'A'}
    store.tracks.upsert_track('imported', 'Imported song', 'Artist', None, 180)
    assert store.rec_ledger.evidence().byte_budget == library_byte_budget(2)
    with store.read_snapshot() as snapshot:
        assert snapshot.store.rec_ledger.evidence().byte_budget == library_byte_budget(2)
    assert library_byte_budget(100_000) > 1024 * 1024 * 1024


def test_capacity_retires_oldest_unanswered_graphs_and_preserves_protected_roots(tmp_path):
    ledger = EvidenceLedger(tmp_path, byte_budget=1_000_000)
    contexts = {}
    for index, name in enumerate(('chosen', 'corrected', 'active', 'pending', 'old', 'newer', 'recent')):
        component = ledger.artifact({'snapshot': name * (5400 if name in {'old', 'newer'} else 1800)})
        context = ledger.artifact({'data': {'$artifact': component}})
        contexts[name] = context
        ledger.put('opportunity', name, {'id': name, 'context': context,
                   'observed_at': 10 + index if name != 'recent' else 9950})
        ledger.put('response', name + '-visible', {'id': name + '-visible',
                   'opportunity_id': name, 'kind': 'visible', 'observed_at': 20 + index})
    for name, kind in (('chosen', 'choice'), ('corrected', 'withdraw')):
        ledger.put('response', name, {'id': name, 'opportunity_id': name,
                                     'kind': kind, 'observed_at': 50})
    for path in tmp_path.glob('artifact/*/*.json'):
        os.utime(path, (100, 100))
    used = sum(path.stat().st_size for path in tmp_path.glob('*/*/*.json'))
    ledger.byte_budget = int(used / .95)
    result = ledger.maintain(0, orphan_before=9000, pressure_before=9000,
                            protected_opportunities={'active', 'pending'})
    assert result['retired_for_capacity'] == 2
    assert result['used_bytes'] <= ledger.byte_budget * .7
    for name in ('chosen', 'corrected', 'active', 'pending', 'recent'):
        assert ledger.get('opportunity', name)
        assert ledger.get('artifact', contexts[name])
    for name in ('old', 'newer'):
        assert ledger.get('opportunity', name) is None
        assert ledger.get('artifact', contexts[name]) is None
        assert ledger.get('response', name + '-visible') is None
    second_ledger = EvidenceLedger(tmp_path, ledger.byte_budget)
    assert second_ledger.put('response', 'later-choice', {'choice': 'kept'})
    assert int((tmp_path / '.budget').read_text()) == sum(
        path.stat().st_size for path in tmp_path.glob('*/*/*.json'))


def test_capacity_never_discards_protected_evidence_when_it_cannot_recover(tmp_path):
    ledger = EvidenceLedger(tmp_path, byte_budget=1000)
    context = ledger.artifact({'snapshot': 'x' * 700})
    ledger.put('opportunity', 'choice', {'id': 'choice', 'context': context, 'observed_at': 1})
    ledger.put('response', 'selected', {'id': 'selected', 'opportunity_id': 'choice',
                                     'kind': 'choice', 'observed_at': 2})
    ledger.byte_budget = 500
    assert ledger.maintain(0, orphan_before=10**12, pressure_before=10**12)['full']
    assert ledger.get('artifact', context) == {'snapshot': 'x' * 700}
    with pytest.raises(EvidenceBudgetExceeded):
        ledger.put('response', 'new', {'choice': 'B'})


def test_pending_write_reclaims_old_optional_graphs_but_preserves_learning_and_shared_roots(tmp_path):
    ledger = EvidenceLedger(tmp_path, byte_budget=100_000)
    shared = ledger.artifact({'common': 'shared inputs' * 10})
    contexts, components, records, checkpoints = {}, {}, {}, {}
    for index, name in enumerate(('old', 'new', 'held', 'unacknowledged')):
        component = ledger.artifact({'data': name[0] * 700})
        context = ledger.artifact({'shared': {'$artifact': shared}, 'private': {'$artifact': component}})
        contexts[name], components[name] = context, component
        record = {'id': name, 'context': context, 'observed_at': index + 1,
                  'comparison': {'run_id': 'active-trial'}}
        ledger.put('opportunity', name, record)
        records[name] = record
        if name != 'unacknowledged':
            value = {'source_context': context, 'learning': name * (4500 if name == 'held' else 20)}
            ledger.put('checkpoint', context, value)
            checkpoints[context] = value
    # Two opportunities sharing a diagnostic graph must release all its optional
    # references together while retaining both original immutable offer records.
    duplicate = {'id': 'old-again', 'context': contexts['old'], 'observed_at': 1.5,
                 'comparison': {'run_id': 'active-trial'}}
    ledger.put('opportunity', 'old-again', duplicate)
    records['old-again'] = duplicate
    for path in tmp_path.glob('artifact/*/*.json'):
        os.utime(path, (1, 1))
    used = sum(path.stat().st_size for path in tmp_path.glob('*/*/*.json'))
    ledger.byte_budget = used + 100
    required_record = {'id': 'choice', 'opportunity_id': 'held', 'kind': 'choice',
                       'observed_at': 11, 'details': 'x' * 300}
    kwargs = dict(protected_artifacts={shared}, protected_runs={'active-trial'},
                  protected_opportunities={'held'}, orphan_before=10,
                  compacted_contexts=set(checkpoints), context_before=0,
                  context_byte_budget=int(ledger.byte_budget * .1))
    ordinary = ledger.maintain(0, **kwargs)
    assert ordinary['used_bytes'] == used and not ordinary['full']
    assert ordinary['optional_context_bytes'] > 0
    with pytest.raises(EvidenceBudgetExceeded) as failure:
        ledger.put('response', 'choice', required_record)
    result = ledger.maintain(0, required_bytes=failure.value.required_bytes, **kwargs)
    assert not result['full'] and result['shortfall_bytes'] == 0
    assert result['available_bytes'] >= len(canonical(required_record))
    assert result['reclaimed_optional_bytes'] > 0
    assert result['retired_unanswered'] == 0
    assert ledger.get('artifact', contexts['old']) is None
    assert ledger.get('artifact', components['old']) is None
    for name in ('new', 'held', 'unacknowledged'):
        assert ledger.get('artifact', contexts[name]) is not None
        assert ledger.get('artifact', components[name]) is not None
    assert ledger.get('artifact', shared) is not None
    for identity, record in records.items():
        assert ledger.get('opportunity', identity) == record
    for identity, value in checkpoints.items():
        assert ledger.get('checkpoint', identity) == value
    assert ledger.put('response', 'choice', required_record)
    restarted = EvidenceLedger(tmp_path, ledger.byte_budget)
    assert restarted.get('response', 'choice') == required_record
    assert int((tmp_path / '.budget').read_text()) == sum(
        path.stat().st_size for path in tmp_path.glob('*/*/*.json'))


def test_pending_write_reports_shortfall_without_releasing_required_context(tmp_path):
    ledger = EvidenceLedger(tmp_path, byte_budget=10_000)
    context = ledger.artifact({'required': 'x' * 1000})
    record = {'id': 'held', 'context': context, 'observed_at': 1}
    ledger.put('opportunity', 'held', record)
    used = sum(path.stat().st_size for path in tmp_path.glob('*/*/*.json'))
    ledger.byte_budget = used + 100
    result = ledger.maintain(0, protected_opportunities={'held'}, orphan_before=100,
                             compacted_contexts={context}, context_before=0,
                             context_byte_budget=10_000, required_bytes=200)
    assert result['full'] and result['shortfall_bytes'] == 100
    assert result['available_bytes'] == 100 and result['reclaimed_optional_bytes'] == 0
    assert ledger.get('opportunity', 'held') == record
    assert ledger.get('artifact', context) == {'required': 'x' * 1000}
