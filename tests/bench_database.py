"""Explicit, serial representative-library profiling; never writes the source database.

TUNECONSOLE_BENCH_DB=/path/to/state.db uv run pytest --test-lock-wait=60 -s tests/bench_database.py
The JSON report contains counts, timings, SQL fingerprints and plans, without SQL text or values.
"""
from collections import defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import db_diagnostics as diagnostics
from yt_playlist.core.activity import WorkClosing, cancellable_work
from yt_playlist.core.store import Store
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.rec import trend_rollups


def test_representative_trend_queries(tmp_path, monkeypatch):
    source = os.environ.get("TUNECONSOLE_BENCH_DB")
    if not source:
        pytest.skip("Set TUNECONSOLE_BENCH_DB to profile a read-only library copy")
    destination = tmp_path / "library.db"
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer, pages=1024)
    store = Store(destination)
    report = {"sqlite": sqlite3.sqlite_version, "operations": {}, "sql": []}
    measurements = defaultdict(lambda: {"calls": 0, "seconds": 0., "plan": []})
    try:
        report["source_schema"] = store.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        store.init_schema()  # upgrades/repairs affect only the disposable copy
        report["profiled_schema"] = store.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        report["counts"] = {table: store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                            for table in ("recordings", "catalog_entries", "playlist_tracks", "play_events",
                                          "listening_observations", "trend_first_play")}
        original = diagnostics.DiagnosticCursor._call

        def measured(cursor, stage, target, *args):
            before = cursor._elapsed
            try:
                return original(cursor, stage, target, *args)
            finally:
                elapsed = cursor._elapsed - before
                key = (diagnostics._operation.get(), hashlib.sha256(cursor._sql.encode()).hexdigest()[:12])
                item = measurements[key]
                item["seconds"] += elapsed
                item["calls"] += 1
                if stage == "execute" and not item["plan"]:
                    # Base cursor avoids recursively measuring EXPLAIN itself. Neither SQL text
                    # nor bound values leave this scope; SQLite's plan names tables and indexes.
                    try:
                        plan = sqlite3.Cursor(cursor.connection).execute("EXPLAIN QUERY PLAN " + args[0], args[1])
                        item["plan"] = [row[3] for row in plan.fetchall()]
                    except sqlite3.Error:
                        pass  # transaction statements have no query plan

        monkeypatch.setattr(diagnostics.DiagnosticCursor, "_call", measured)

        def run(name, target):
            # Bound pathological queries without sleeps, retries or changes to the live server.
            deadline = time.monotonic() + 120
            started = time.monotonic()
            try:
                with work_span(name), cancellable_work(lambda: time.monotonic() >= deadline):
                    target()
                outcome = "ok"
            except WorkClosing:
                outcome = "interrupted after 120s"
            report["operations"][name] = {"seconds": round(time.monotonic() - started, 4), "outcome": outcome}

        run("first-play refresh", store.trends.refresh_first_play)
        run("artist ledger", lambda: store.trends.ledger_artist_plays(0, time.time()))
        run("trend rollups", lambda: trend_rollups.build(store, time.time()))
        if os.environ.get("TUNECONSOLE_BENCH_BASELINE") == "1":
            # Read-only reference shapes from the original Trends implementation plans.
            # Compare the repeated view lookups without replacing current application code.
            def reference(name, sql, values=()):
                with diagnostics.database_operation(name):
                    store.conn.execute(sql, values).fetchall()

            run("legacy artist source lookup", lambda: reference("legacy artist source lookup", """
                SELECT t.artist,MIN(fp.first_day),MIN(fp.first_ts),
                  (SELECT fp2.source FROM trend_first_play fp2
                   JOIN tracks t2 ON t2.identity_key=fp2.id_key
                   WHERE fp2.kind='track' AND t2.artist=t.artist
                   ORDER BY fp2.first_ts LIMIT 1)
                FROM trend_first_play fp JOIN tracks t ON t.identity_key=fp.id_key
                WHERE fp.kind='track' AND t.artist<>'' GROUP BY t.artist"""))
            frag, values = store.trends._generated_filter(True)
            run("legacy artist ledger", lambda: reference("legacy artist ledger",
                "SELECT a,COUNT(*) FROM ("
                "SELECT (SELECT MIN(t.artist) FROM tracks t WHERE t.identity_key=pe.identity_key) a "
                f"FROM play_events pe WHERE pe.played_at>=? AND pe.played_at<?{frag}) "
                "WHERE a IS NOT NULL AND a<>'' GROUP BY a", [0, time.time(), *values]))
    finally:
        report["sql"] = [dict(operation=operation, sql_id=sql_id, calls=item["calls"],
                              seconds=round(item["seconds"], 4), plan=item["plan"])
                         for (operation, sql_id), item in sorted(measurements.items(),
                             key=lambda pair: pair[1]["seconds"], reverse=True)]
        store.conn.close()
        output = Path(os.environ.get("TUNECONSOLE_BENCH_REPORT", str(tmp_path / "database-profile.json")))
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(output), "counts": report.get("counts"),
                          "operations": report["operations"], "slowest": [
                              {k: row[k] for k in ("operation", "sql_id", "seconds")}
                              for row in report["sql"][:10]]}), flush=True)
    assert all(item["outcome"] == "ok" for item in report["operations"].values())
