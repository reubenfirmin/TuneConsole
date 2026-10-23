"""Priority selection yields to serving work and admits only current recording jobs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest

from yt_playlist.core.store import Store


@pytest.fixture
def database(tmp_path):
    store = Store(tmp_path / "queue.db")
    store.init_schema()
    store.settings.set_setting("enrich_caught_up_at", "100")
    store.enrichment.reconcile_provider_configuration({
        "deezer": {"fingerprint": "original", "status": "available"}})
    for video, title in (("one", "First"), ("two", "Second")):
        store.tracks.upsert_track(video, title, "Artist", None, 200, created_at=1)
    yield store
    store.conn.close()


def test_queue_calculation_allows_foreground_reads_and_writes(database, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = database.read_snapshot

    @contextmanager
    def paused_reader():
        with original() as snapshot:
            assert snapshot.store.conn is not database.conn

            def progress():
                entered.set()
                assert release.wait(5), "Serving request was blocked by priority selection"
                return 0

            snapshot.store.conn.set_progress_handler(progress, 1)
            yield snapshot

    monkeypatch.setattr(database, "read_snapshot", paused_reader)
    with ThreadPoolExecutor(2) as pool:
        selection = pool.submit(database.enrichment.next_enrich_batch, 2, providers=["deezer"], now=1000)
        try:
            assert entered.wait(5)

            def foreground():
                database.settings.set_setting("interactive", "saved")
                return database.settings.get_setting("interactive")

            assert pool.submit(foreground).result(2) == "saved"
        finally:
            release.set()
        assert [t["title"] for t in selection.result(5)] == ["First", "Second"]
    assert not database.conn.in_transaction


@pytest.mark.parametrize("change", [
    "completed", "retry", "cooldown", "removed", "input_changed", "pass_changed",
    "provider_disabled", "provider_reconfigured", "refresh_completed",
])
def test_snapshot_candidates_are_revalidated_before_job_admission(database, monkeypatch, change):
    first = dict(database.conn.execute("SELECT * FROM catalog_tracks WHERE video_id='one'").fetchone())
    if change == "refresh_completed":
        database.enrichment.mark_recordings_enriched([first["recording_id"]], 10)
        database.enrichment.reconcile_provider_configuration({
            "deezer": {"fingerprint": "refresh", "status": "available"}})
    original = database.read_snapshot

    @contextmanager
    def changed_reader():
        with original() as snapshot:
            yield snapshot
            if change in ("completed", "pass_changed"):
                database.enrichment.mark_recordings_enriched([first["recording_id"]], 1001)
                if change == "pass_changed":
                    # A new refresh belongs to a different pass, even with the same input.
                    with database.conn:
                        database.conn.execute("INSERT OR IGNORE INTO enrichment_provider_jobs VALUES (?,'deezer')",
                                              (first["recording_id"],))
            elif change in ("retry", "cooldown"):
                database.enrichment.record_enrichment_attempt(first["identity_key"], "deezer", {},
                    retry=True, cooldown=change == "cooldown", now=1000)
            elif change == "removed":
                with database.conn:
                    database.conn.execute("DELETE FROM library_memberships WHERE entry_id=?", (first["id"],))
            elif change == "input_changed":
                replacement = database.tracks.upsert_track("replacement", "First", "Artist", None, 200)
                assert replacement != first["id"]
                with database.conn:
                    database.conn.execute("DELETE FROM library_memberships WHERE entry_id=?", (first["id"],))
            elif change.startswith("provider_"):
                database.enrichment.reconcile_provider_configuration({"deezer": {
                    "fingerprint": "changed", "status": "disabled" if change == "provider_disabled" else "available"}})
            elif change == "refresh_completed":
                database.enrichment.record_enrichment_attempt(first["identity_key"], "deezer", {}, now=1000)

    monkeypatch.setattr(database, "read_snapshot", changed_reader)
    batch = database.enrichment.next_enrich_batch(2, providers=["deezer"], now=1000)
    expected = [] if change in ("cooldown", "provider_disabled", "provider_reconfigured") else ["Second"]
    assert [t["title"] for t in batch] == expected
    assert not database.conn.in_transaction


def test_snapshot_reader_retains_its_own_pinned_queue(database):
    with database.read_snapshot() as snapshot:
        first = dict(database.conn.execute("SELECT * FROM catalog_tracks WHERE video_id='one'").fetchone())
        database.enrichment.mark_recordings_enriched([first["recording_id"]], 1000)
        assert [t["title"] for t in snapshot.store.enrichment.next_enrich_batch(2)] == ["First", "Second"]
    assert [t["title"] for t in database.enrichment.next_enrich_batch(2)] == ["Second"]
