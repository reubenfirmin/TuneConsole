"""Opt-in, serial #287 profiling against an already sanitized, disposable snapshot.

TUNECONSOLE_ENRICH_BENCH_SNAPSHOT must name a prepared copy, never the live database.
Only this test's temporary copy is upgraded or modified. Reports retain aggregate
cardinalities, SQL plans/fingerprints, result digests and timings, without music metadata.
"""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from yt_playlist.core import db_diagnostics
from yt_playlist.core.activity import cancellable_work
from yt_playlist.core.store import Store


def test_priority_selection_profile(tmp_path, monkeypatch):
    source = os.environ.get("TUNECONSOLE_ENRICH_BENCH_SNAPSHOT")
    if not source:
        pytest.skip("Set TUNECONSOLE_ENRICH_BENCH_SNAPSHOT to an existing sanitized copy")
    source = Path(source).resolve()
    assert source.name != "state.db", "Use a sanitized disposable snapshot, never the live database"
    destination = tmp_path / "enrichment-profile.db"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer, pages=1024)
    store = Store(destination)
    report = {"sqlite": sqlite3.sqlite_version, "operations": {}, "sql": []}
    measurements = defaultdict(lambda: {"stages": defaultdict(float), "calls": 0, "plan": []})
    label = None
    entered = threading.Event()
    selected_connection = None
    original = db_diagnostics.DiagnosticCursor._call
    def measured(cursor, stage, target, *args):
        nonlocal selected_connection
        before = cursor._elapsed
        if stage == "execute" and "queue AS (" in cursor._sql:
            selected_connection = cursor.connection
            entered.set()
        try:
            return original(cursor, stage, target, *args)
        finally:
            if label is not None:
                sql_id = hashlib.sha256(cursor._sql.encode()).hexdigest()[:12]
                item = measurements[(label, sql_id, cursor.connection is store.conn)]
                item["stages"][stage] += cursor._elapsed - before
                item["calls"] += 1
                if stage == "execute" and not item["plan"]:
                    try:
                        plan = sqlite3.Cursor(cursor.connection).execute("EXPLAIN QUERY PLAN " + args[0], args[1])
                        item["plan"] = [r[3] for r in plan.fetchall()]
                    except sqlite3.Error:
                        pass

    try:
        store.init_schema()
        # The pre-fix path is retained as the exact ranking calculation, so measure
        # it under the serving lock without duplicating its SQL.
        select = (store.enrichment._select_enrich_batch
                  if os.environ.get("TUNECONSOLE_ENRICH_BENCH_LOCKED") == "1"
                  else store.enrichment.next_enrich_batch)
        # Model the current catalog-wide genre policy refresh, retaining real
        # memberships, container sizes and listening-day distributions.
        with store.conn:
            store.conn.execute("INSERT OR REPLACE INTO enrichment_provider_config VALUES ('deezer','profile','available')")
            store.conn.execute("INSERT OR IGNORE INTO enrichment_provider_jobs "
                               "SELECT id,'deezer' FROM recordings WHERE first_enriched_at IS NOT NULL")
        report["counts"] = {table: store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("recordings", "catalog_entries", "catalog_enrichment_tracks", "playlist_tracks",
                          "album_tracks", "listening_days", "enrichment_provider_jobs")}
        monkeypatch.setattr(db_diagnostics.DiagnosticCursor, "_call", measured)

        def run(name, target):
            nonlocal label
            samples, result = [], None
            label = name
            try:
                for _ in range(3):
                    started = time.monotonic()
                    with cancellable_work(lambda: time.monotonic() - started >= 120):
                        result = target()
                    samples.append(time.monotonic() - started)
            finally:
                label = None
            report["operations"][name] = {"seconds": samples, "result_size": len(result),
                "result_digest": hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()}

        # Isolate the account-wide CTEs before changing indexes or selection.
        for name, sql in (
                ("listening_totals", "SELECT COUNT(*),SUM(c) FROM plays"),
                ("playlist_totals", "SELECT COUNT(*),SUM(c) FROM pl_plays"),
                ("album_totals", "SELECT COUNT(*),SUM(c) FROM al_plays"),
                ("artist_totals", "SELECT COUNT(*),SUM(c) FROM ar_plays")):
            run(name, lambda sql=sql: [tuple(r) for r in store.conn.execute(store.enrichment._QUEUE_CTES + sql)])
        for name, limit, offset in (("first_40", 40, 0), ("offset_40", 40, 40), ("all", -1, 0)):
            run(name, lambda limit=limit, offset=offset: select(
                limit, offset, providers=["deezer"], now=2_000_000_000))

        # Ask for an ordinary foreground setting while the actual priority SQL
        # executes. No artificial delay is added to the measured queue query.
        waits = []
        with ThreadPoolExecutor(1) as worker:
            for _ in range(3):
                entered.clear()
                future = worker.submit(select, 40,
                                       providers=["deezer"], now=2_000_000_000)
                assert entered.wait(10), "Priority query did not start"
                started = time.monotonic()
                assert isinstance(store.settings.get_setting("enrich_caught_up_at", ""), str)
                waits.append(time.monotonic() - started)
                future.result(120)
        report["foreground_wait_seconds"] = waits
        report["priority_query_uses_serving_connection"] = selected_connection is store.conn
        baseline = os.environ.get("TUNECONSOLE_ENRICH_BENCH_COMPARE")
        if baseline:
            previous = json.loads(Path(baseline).read_text())
            for name, values in previous["operations"].items():
                assert values["result_digest"] == report["operations"][name]["result_digest"], name
            report["identical_results"] = list(previous["operations"])
    finally:
        report["sql"] = [{"label": name, "sql_id": sql_id, "serving_connection": serving,
                          "calls": item["calls"], "stages": dict(item["stages"]), "plan": item["plan"]}
                         for (name, sql_id, serving), item in measurements.items()]
        store.conn.close()
        output = Path(os.environ.get("TUNECONSOLE_ENRICH_BENCH_REPORT", str(tmp_path / "enrichment-profile.json")))
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(output), "counts": report.get("counts"),
                          "operations": report["operations"],
                          "foreground_wait_seconds": report.get("foreground_wait_seconds")}), flush=True)
