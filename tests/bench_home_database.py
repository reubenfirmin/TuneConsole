"""Opt-in, serial profiling for #220; source databases are opened read-only.

Set TUNECONSOLE_BENCH_DB to the source and TUNECONSOLE_BENCH_SNAPSHOT to a new
private path. Account credentials/configuration are stripped from that snapshot;
music and listening relationships stay local so the query shapes remain real.
Reuse the snapshot for before/after runs. Reports contain only aggregate sizes,
result digests, timings, SQL fingerprints and plans, never metadata or bindings.
"""
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import db_diagnostics, priority
from yt_playlist.core.activity import cancellable_work
from yt_playlist.core.migrations.current import SEED_SETTINGS
from yt_playlist.core.migrations import v026_listening_observations
from yt_playlist.core.store import Store


def _copy(source, destination):
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer, pages=1024)


def _snapshot(source, destination):
    _copy(source, destination)
    destination.chmod(0o600)
    with closing(sqlite3.connect(destination)) as conn, conn:
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("UPDATE identities SET label='account-'||id,credential_ref='redacted',brand_account_id=NULL")
        conn.execute("DELETE FROM settings WHERE key NOT IN (" + ",".join("?" for _ in SEED_SETTINGS) + ")",
                     tuple(SEED_SETTINGS))
        conn.execute("DELETE FROM identity_state_archive")
        conn.execute("DELETE FROM actions")
        conn.execute("DELETE FROM enrichment_provider_config")
    # Remove deleted credentials from free pages too. This is a disposable copy.
    with closing(sqlite3.connect(destination)) as conn:
        conn.execute("VACUUM")


def test_representative_home_queries(tmp_path, monkeypatch):
    snapshot = os.environ.get("TUNECONSOLE_BENCH_SNAPSHOT")
    if not snapshot:
        pytest.skip("Set TUNECONSOLE_BENCH_SNAPSHOT for explicit database profiling")
    snapshot = Path(snapshot)
    source = os.environ.get("TUNECONSOLE_BENCH_DB")
    assert not source or snapshot.resolve() != Path(source).resolve()
    if not snapshot.exists():
        assert source, "A source database is required to create the private snapshot"
        _snapshot(source, snapshot)
    destination = tmp_path / "home-profile.db"
    _copy(snapshot, destination)
    store = Store(destination)
    report = {"sqlite": sqlite3.sqlite_version, "operations": {}, "sql": []}
    measurements = defaultdict(lambda: {"stages": defaultdict(float), "calls": 0, "plan": []})
    label = None
    lock_seconds = 0.
    explain_seconds = 0.
    try:
        report["source_schema"] = store.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        store.init_schema()  # upgrades only this run's disposable copy
        report["profiled_schema"] = store.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        report["counts"] = {table: store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("recordings", "catalog_entries", "library_memberships", "playlist_items",
                          "play_events", "listening_observations", "rec_served_impressions",
                          "rec_response_events", "rec_serving_policies")}
        keys = [r[0] for r in store.conn.execute(
            "SELECT DISTINCT r.identity_key FROM recordings r JOIN catalog_entries e ON e.recording_id=r.id "
            "JOIN library_memberships m ON m.entry_id=e.id ORDER BY r.identity_key LIMIT 120")]
        offers = set()

        def collect(value):
            if isinstance(value, dict):
                key = value.get("identity_key") or value.get("key")
                if isinstance(key, str):
                    offers.add(key)
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        menu_sizes, discovery_sizes = [], []
        for row in store.conn.execute("SELECT payload FROM rec_proposals WHERE surface IN ('home_prepared','home_tuned')"):
            payload = json.loads(row[0])
            collect(payload)
            for menu in payload.get('menus', []):
                menu_keys = {t['key'] for p in menu.get('protos', [])
                             for t in [*p['tracks'], *(p.get('counterfactual') or {}).get('tracks', [])]}
                menu_sizes.append(len(menu_keys))
            discovery_sizes.append(len(payload.get('discovery_tracks', [])))
        report["prepared_offer_keys"] = len(offers)
        report['prepared_menu_key_counts'] = menu_sizes
        report['prepared_discovery_key_counts'] = discovery_sizes
        playlist = store.conn.execute(
            "SELECT playlist_id,COUNT(*) n FROM playlist_tracks GROUP BY playlist_id "
            "ORDER BY ABS(COUNT(*)-60),playlist_id LIMIT 1").fetchone()
        report["playlist_size"] = playlist["n"] if playlist else 0
        original = db_diagnostics.DiagnosticCursor._call
        original_lock = priority.database_lock_wait

        def measured(cursor, stage, target, *args):
            nonlocal explain_seconds
            before = cursor._elapsed
            try:
                return original(cursor, stage, target, *args)
            finally:
                if label is not None:
                    item = measurements[(label, db_diagnostics._operation.get(),
                                         hashlib.sha256(cursor._sql.encode()).hexdigest()[:12])]
                    item["stages"][stage] += cursor._elapsed - before
                    item["calls"] += 1
                    if stage == "execute" and not item["plan"]:
                        started = time.monotonic()
                        try:
                            plan = sqlite3.Cursor(cursor.connection).execute("EXPLAIN QUERY PLAN " + args[0], args[1])
                            item["plan"] = [r[3] for r in plan.fetchall()]
                        except sqlite3.Error:
                            pass
                        finally:
                            explain_seconds += time.monotonic() - started

        def measured_lock(acquire, *args):
            nonlocal lock_seconds
            started = time.monotonic()
            try:
                return original_lock(acquire, *args)
            finally:
                lock_seconds += time.monotonic() - started

        monkeypatch.setattr(db_diagnostics.DiagnosticCursor, "_call", measured)
        monkeypatch.setattr(priority, "database_lock_wait", measured_lock)

        def run(name, target, input_size=None):
            nonlocal label, lock_seconds, explain_seconds
            samples, result = [], None
            label, lock_seconds, explain_seconds = name, 0., 0.
            for _ in range(3):
                started = time.monotonic()
                with cancellable_work(lambda: time.monotonic() - started >= 120):
                    result = target()
                samples.append(time.monotonic() - started)
            label = None
            sql_seconds = sum(sum(m['stages'].values()) for (n, _, _), m in measurements.items() if n == name)
            report["operations"][name] = {
                "seconds": [round(s, 6) for s in samples], "input_size": input_size,
                "lock_wait_seconds": round(lock_seconds, 6),
                "sqlite_seconds": round(sql_seconds, 6),
                "explain_seconds": round(explain_seconds, 6),
                # Includes dictionary/JSON conversion and profiling overhead;
                # SQLite result-row construction is already in fetch timings.
                "python_conversion_and_overhead_seconds": round(max(0., sum(samples) - sql_seconds - lock_seconds - explain_seconds), 6),
                "result_size": len(result) if result is not None else None,
                "result_digest": hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()}

        for size in (10, 40, 120):
            run(f"tracks_by_keys_{size}", lambda size=size: store.rec_query.tracks_by_keys(keys[:size]), min(size, len(keys)))
        if offers:
            selected = sorted(offers)[:200]
            run("tracks_by_keys_prepared", lambda: store.rec_query.tracks_by_keys(selected), len(selected))
        run("play_counts", store.rec_query.play_counts)
        run("genre_play_distribution", store.rec_query.genre_play_distribution)
        run("era_play_distribution", store.rec_query.era_play_distribution)

        def separate_summaries():
            plays = store.rec_query.play_counts()
            # The old preparation independently built options, breadth, eras,
            # and the saved genre distribution from the same generation.
            return {'plays': plays, 'options': store.rec_query.genre_play_distribution(),
                    'breadth': store.rec_query.genre_play_distribution(),
                    'eras': store.rec_query.era_play_distribution(),
                    'genres': store.rec_query.genre_play_distribution()}

        def shared_summaries():
            plays = store.rec_query.play_counts()
            facets = store.rec_query.play_weighted_facets(plays)
            return {'plays': plays, 'options': facets['genres'], 'breadth': facets['genres'],
                    'eras': facets['eras'], 'genres': facets['genres']}

        run('home_summaries_separate', separate_summaries)
        run('home_summaries_shared', shared_summaries)
        run("recent_plays_with_ts", store.history.recent_plays_with_ts)
        run("get_playlist_listen_stats", store.charts.get_playlist_listen_stats)
        if playlist:
            run("playlist_tracks_detail", lambda: store.charts.playlist_tracks_detail(playlist["playlist_id"]), playlist["n"])
        now = store.conn.execute("SELECT MAX(served_at) FROM rec_served_impressions").fetchone()[0] or time.time()
        run("impression_retention", lambda: store.rec_surface.record_served_impressions(
            "profile-retention", "home", [], now, prune_before=now - 90 * 86400), 0)
        original_evidence = v026_listening_observations.PROJECTIONS[2]
        original_evidence = original_evidence[original_evidence.index(' AS') + 3:]
        expected = Counter(map(tuple, store.conn.execute(original_evidence)))
        actual = Counter(map(tuple, store.conn.execute('SELECT * FROM eligible_listening_evidence')))
        assert actual == expected
        report['evidence_rows_equal'] = True
        report['evidence_row_count'] = sum(actual.values())
        assert report['operations']['home_summaries_separate']['result_digest'] == report['operations']['home_summaries_shared']['result_digest']
        baseline = os.environ.get('TUNECONSOLE_BENCH_COMPARE')
        if baseline:
            previous = json.loads(Path(baseline).read_text())
            compared = []
            for name, result in previous['operations'].items():
                if name in report['operations']:
                    assert result['result_digest'] == report['operations'][name]['result_digest'], name
                    compared.append(name)
            report['identical_results'] = compared
    finally:
        report["sql"] = [dict(label=name, operation=operation, sql_id=sql_id, calls=item["calls"],
                              stages={k: round(v, 6) for k, v in item["stages"].items()}, plan=item["plan"])
                         for (name, operation, sql_id), item in measurements.items()]
        store.conn.close()
        output = Path(os.environ.get("TUNECONSOLE_BENCH_REPORT", str(tmp_path / "home-profile.json")))
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(output), "counts": report.get("counts"),
                          "operations": report["operations"]}), flush=True)
