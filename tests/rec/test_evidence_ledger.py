from concurrent.futures import ThreadPoolExecutor
import os

import pytest

from yt_playlist.core.evidence_ledger import (
    EvidenceBudgetExceeded, EvidenceConflict, EvidenceLedger, library_byte_budget,
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
