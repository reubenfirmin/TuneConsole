"""Opt-in full SVD benchmark on a disposable copy of the saved library.

uv run pytest --test-lock-wait=60 -s tests/bench_svd_background.py
TUNECONSOLE_BENCH_DB overrides the development database. Each measurement runs
in a fresh interpreter, with production's one-thread numerical limit. The source
is opened read-only; schema changes and vector publication affect only the copy.
"""
import asyncio
from contextlib import closing
import faulthandler
from functools import wraps
import json
import os
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
import threading
import time

import httpx
import numpy as np
import pytest
from threadpoolctl import threadpool_info

from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core import db_diagnostics, db_snapshot
from yt_playlist.core.db_snapshot import ReadSnapshot
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.store import Store
from yt_playlist.rec import embed, mode_surfaces, model_refresh, trend_rollups
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app


class LockProbe:
    """Record outer serving-lock holds on the fitting thread, excluding checkpoint time."""
    def __init__(self, lock):
        self.lock = lock
        self.owner = None
        self.local = threading.local()
        self.holds = []

    def __enter__(self):
        self.lock.acquire()
        depth = getattr(self.local, 'depth', 0)
        if not depth:
            self.local.started = (time.monotonic(), time.thread_time())
        self.local.depth = depth + 1
        return self

    def __exit__(self, *args):
        self.local.depth -= 1
        if not self.local.depth and threading.get_ident() == self.owner:
            wall, cpu = self.local.started
            self.holds.append({'wall_s': time.monotonic() - wall, 'cpu_s': time.thread_time() - cpu})
        self.lock.release()


def measure(path, *, full_generation=False, refresh=None, projection=None, inline_checkpoint=False):
    limit_native_threads()
    store = Store(path)
    store.init_schema()
    # A partial/unchanged refresh starts from a compatible, up-to-date model on the disposable
    # copy. This warm-up is excluded from the measured background task and contention probes.
    if refresh:
        assert model_refresh.rebuild(store, time.time()) is not None
        if refresh == 'partial':
            title, artist = 'Publication probe song', 'Publication probe artist'
            store.discovery.upsert_discovered_track(identity_key(title, artist), 'publication-probe',
                title, artist, '', None, 'Techno', None, None, time.time())
    lock_probe = LockProbe(store._lock)
    store._lock = lock_probe
    app = create_app(store, lambda: {}, background_work=False)
    stages = {}
    active_stage = "starting"
    scale = 1 if sys.platform == "darwin" else 1024
    baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale

    def timed(name, target):
        @wraps(target)
        def run(*args, **kwargs):
            nonlocal active_stage
            if lock_probe.owner is not None and threading.get_ident() != lock_probe.owner:
                return target(*args, **kwargs)
            previous = active_stage
            active_stage = name
            started, cpu = time.monotonic(), time.thread_time()
            try:
                return target(*args, **kwargs)
            finally:
                stages[name] = {"wall_s": time.monotonic() - started,
                                "cpu_s": time.thread_time() - cpu}
                active_stage = previous
        return run

    # Wrap the production operations without replacing their inputs or math.
    embed._cooc_matrix = timed("cooccurrence", embed._cooc_matrix)
    embed._positive_pmi = timed("ppmi", embed._positive_pmi)
    np.linalg.qr = timed("qr", np.linalg.qr)
    np.linalg.svd = timed("svd", np.linalg.svd)
    db_diagnostics.DiagnosticConnection.commit = timed('commit', db_diagnostics.DiagnosticConnection.commit)
    db_snapshot.checkpoint_wal = timed('checkpoint', db_snapshot.checkpoint_wal)
    Revisions.capture = timed('revision_capture', Revisions.capture)
    RecModelRepo.prepare_training_changes = timed('vector_comparison', RecModelRepo.prepare_training_changes)
    RecModelRepo.publish_training_generation = timed('vector_writes', RecModelRepo.publish_training_generation)
    metadata = {"method": "svd", "native_threads": [
        {"api": lib["internal_api"], "threads": lib["num_threads"]}
        for lib in threadpool_info()]}
    if full_generation or refresh or projection:
        # Only the disposable backup is changed. Force all model spaces and modes
        # through production preparation/publication, including unchanged inputs.
        if full_generation:
            for key in ("rec_fit_inputs", "rec_content_inputs", "rec_artist_fit_inputs", "rec_mode_fit_inputs"):
                store.settings.delete_setting(key)
        original_publish = ReadSnapshot.publish

        def publish(snapshot, target):
            return original_publish(snapshot, target, defer_checkpoint=not inline_checkpoint)

        ReadSnapshot.publish = timed("publication", publish)
        metadata["method"] = (f'{projection} projection' if projection else
                              f'{refresh or "complete"} production model generation')
        metadata['inline_checkpoint'] = inline_checkpoint

    def fit():
        lock_probe.owner = threading.get_ident()
        limit_native_threads()
        started, cpu = time.monotonic(), time.thread_time()
        if projection:
            build = {'trends': trend_rollups.build, 'bundles': mode_surfaces.prepare_bundles}[projection]
            prepared = timed('projection', build)(store, float(os.environ.get('TUNECONSOLE_BENCH_NOW', time.time())))
            metadata.update(published=prepared is not None, tracks=store.rec_model.rec_vectors_count(),
                            background_wall_s=time.monotonic() - started,
                            background_cpu_s=time.thread_time() - cpu)
            return
        if full_generation or refresh:
            prepared = timed("full_generation", model_refresh.rebuild)(store, time.time())
            metadata.update(published=prepared is not None,
                            tracks=store.rec_model.rec_vectors_count(),
                            vector_spaces=sorted(prepared["vectors"]) if prepared else [],
                            published_modes=prepared is not None and prepared["modes"] is not None,
                            vector_changes={table: {'upserts': len(delta.upserts), 'removed': len(delta.removed)}
                                            for table, delta in prepared['vector_changes'].items()} if prepared else {},
                            background_wall_s=time.monotonic() - started,
                            background_cpu_s=time.thread_time() - cpu)
            return
        with store.read_snapshot() as snapshot:
            reader = snapshot.store
            dim = int(reader.settings.get_setting("rec_dim") or embed.DIM)
            baskets = timed("basket_loading", reader.rec_query.rec_baskets)()
            weights = (embed._cooc_weights(reader)
                       if reader.settings.get_setting("rec_cooc_weighting") == "1" else None)
            metadata.update(dim=dim, baskets=len(baskets),
                            memberships=sum(map(len, baskets)),
                            largest_basket=max(map(len, baskets), default=0), weighted=weights is not None,
                            configured_method=reader.settings.get_setting("rec_embed_method") or "svd")
            keys, vectors = timed("full_fit", embed.build_vectors_from_baskets)(
                baskets, dim=dim, method="svd", weights=weights)
            metadata["tracks"] = len(keys)
            rows = timed("serialization", lambda: [
                (key, vectors[i].tobytes()) for i, key in enumerate(keys)])()
            changes = reader.rec_model.prepare_training_changes({'rec_vectors': rows})
            metadata['vector_changes'] = {table: {'upserts': len(delta.upserts), 'removed': len(delta.removed)}
                                          for table, delta in changes.items()}
            metadata["published"] = timed("publication", snapshot.publish)(
                lambda: store.rec_model.publish_training_generation(changes))
        metadata.update(background_wall_s=time.monotonic() - started,
                        background_cpu_s=time.thread_time() - cpu)

    async def exercise():
        lag, reads = [], []
        # Separate first-use template/route setup from contention caused by the fit.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            baseline_home = []
            for _ in range(3):
                before = time.monotonic()
                response = await client.get("/home/cards")
                assert response.status_code == 200
                baseline_home.append(time.monotonic() - before)
            metadata.update(cold_home_ms=baseline_home[0] * 1000,
                            baseline_home_max_ms=max(baseline_home[1:]) * 1000)
        task = asyncio.create_task(asyncio.to_thread(fit))

        async def probe_home():
            latencies = []
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1") as client:
                while True:
                    before = time.monotonic()
                    response = await client.get("/home/cards")
                    assert response.status_code == 200
                    latencies.append(time.monotonic() - before)
                    if task.done():
                        break
                    await asyncio.sleep(0.1)
            return latencies

        async def probe_database():
            while True:
                observed_stage = active_stage
                before = time.monotonic()
                await asyncio.to_thread(store.rec_model.rec_vectors_count)
                reads.append((time.monotonic() - before, observed_stage))
                if task.done():
                    break
                await asyncio.sleep(0.01)

        home_task = asyncio.create_task(probe_home())
        read_task = asyncio.create_task(probe_database())
        while not task.done():
            deadline = time.monotonic() + 0.01
            await asyncio.sleep(0.01)
            observed_stage = active_stage
            lag.append((max(0, time.monotonic() - deadline), observed_stage))
        await task
        home = await home_task
        await read_task
        metadata.update(
            event_loop_samples=len(lag), foreground_reads=len(reads),
            home_requests=len(home),
            home_p99_ms=float(np.percentile(home, 99)) * 1000,
            home_max_ms=max(home) * 1000,
            event_loop_p99_ms=float(np.percentile([x[0] for x in lag], 99)) * 1000,
            event_loop_max_ms=max(x[0] for x in lag) * 1000,
            event_loop_worst_stage=max(lag)[1],
            foreground_read_p99_ms=float(np.percentile([x[0] for x in reads], 99)) * 1000,
            foreground_read_max_ms=max(x[0] for x in reads) * 1000,
            foreground_read_worst_stage=max(reads)[1])

    try:
        asyncio.run(exercise())
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
        metadata.update(peak_rss_mib=peak / 2**20,
                        incremental_peak_mib=(peak - baseline) / 2**20, stages=stages,
                        serving_lock_holds=lock_probe.holds,
                        serving_lock_max_ms=max((h['wall_s'] for h in lock_probe.holds), default=0) * 1000)
        return metadata
    finally:
        store.conn.close()


def _saved_library_source():
    return Path(os.environ.get('TUNECONSOLE_BENCH_DB',
                               str(Path.home() / '.local/share/yt-playlist/state.db')))


def _backup_library(source, destination):
    if not source.exists():
        pytest.skip("No saved library; set TUNECONSOLE_BENCH_DB")
    started = time.monotonic()
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
        # Pin a WAL read transaction: a busy live writer otherwise keeps restarting
        # SQLite's paged backup, which can take arbitrarily long to finish.
        reader.execute("BEGIN")
        source_schema = reader.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer, pages=1024)
    return {'backup_s': time.monotonic() - started, 'source_schema': source_schema}


def _saved_library_run(tmp_path, *, full_generation=False, refresh=None, projection=None, inline_checkpoint=False):
    destination = tmp_path / "library.db"
    backup = _backup_library(_saved_library_source(), destination)
    print(json.dumps(backup), flush=True)
    command = [sys.executable, str(Path(__file__).resolve()), str(destination)]
    if full_generation:
        command.append("--full-generation")
    if refresh:
        command.append(f'--refresh={refresh}')
    if projection:
        command.append(f'--projection={projection}')
    if inline_checkpoint:
        command.append('--inline-checkpoint')
    child = subprocess.run(command,
                           capture_output=True, text=True, timeout=180, check=True)
    result = json.loads(child.stdout)
    result.update(backup)
    report = Path(os.environ.get("TUNECONSOLE_BENCH_REPORT", str(tmp_path / "svd-background.json")))
    report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"report": str(report), **result}), flush=True)
    assert result["published"]
    assert result["tracks"] > 0
    assert result["foreground_reads"] > 0
    assert result["home_requests"] > 0
    assert all(lib["threads"] == 1 for lib in result["native_threads"])
    if full_generation:
        assert result["vector_spaces"] == sorted([
            "rec_vectors", "rec_content_vectors", "rec_discovered_content_vectors",
            "rec_artist_vectors", "rec_artist_content_vectors"])
        assert result["published_modes"]
    if refresh == 'unchanged':
        assert result['vector_spaces'] == []
        assert result['vector_changes'] == {}
        assert not result['published_modes']
    if refresh == 'partial':
        assert 'rec_vectors' not in result['vector_spaces']
        assert 'rec_discovered_content_vectors' in result['vector_spaces']
    # Catch accidentally running training on the event loop. Full-generation
    # publication latency is reported above; it has no agreed 250 ms budget.
    assert result["event_loop_max_ms"] < 250
    if not full_generation and not refresh and not projection:
        assert result["foreground_read_max_ms"] < 250
    return result


def test_saved_library_full_svd_in_background(tmp_path):
    _saved_library_run(tmp_path)


def test_saved_library_complete_generation_in_background(tmp_path):
    _saved_library_run(tmp_path, full_generation=True)


@pytest.mark.parametrize('refresh', ['partial', 'unchanged'])
def test_saved_library_refresh_in_background(tmp_path, refresh):
    _saved_library_run(tmp_path, refresh=refresh)


@pytest.mark.parametrize('projection', ['trends', 'bundles'])
def test_compare_saved_library_projection_publications(tmp_path, monkeypatch, projection):
    """Compare ordinary publications with concurrent Home/DB reads and unchanged inputs."""
    frozen = tmp_path / 'frozen.db'
    _backup_library(_saved_library_source(), frozen)
    monkeypatch.setenv('TUNECONSOLE_BENCH_DB', str(frozen))
    monkeypatch.setenv('TUNECONSOLE_BENCH_NOW', str(time.time()))
    results = []
    # Each child gets a fresh backup of the same database and uses the same calculation time.
    for inline in (True, False, False, True):
        result = _saved_library_run(tmp_path, projection=projection, inline_checkpoint=inline)
        results.append({'path': 'inline' if inline else 'deferred', **result})
    report = tmp_path / f'projection-{projection}.json'
    report.write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps({'projection': projection, 'report': str(report), 'results': results}), flush=True)


@pytest.mark.parametrize('scenario', ['full', 'partial', 'unchanged'])
def test_compare_saved_library_publication_boundaries(tmp_path, monkeypatch, scenario):
    """Attribute publication cost using matched saved-library payloads and writer APIs.

    This is a diagnostic comparison, not a new latency acceptance threshold. All
    writes affect a disposable backup. The old writer bodies still present in
    RecModelRepo match HEAD's delete/insert/commit implementations.
    """
    destination = tmp_path / "publication.db"
    _backup_library(_saved_library_source(), destination)
    store = Store(destination)
    store.init_schema()
    try:
        with store.read_snapshot() as snapshot:
            reader = snapshot.store
            content = reader.rec_model.content_generation()
            vectors = {
                "rec_vectors": reader.rec_model.get_rec_vectors(),
                "rec_content_vectors": content["library"],
                "rec_discovered_content_vectors": content["discovered"],
                "rec_artist_vectors": reader.rec_model.get_rec_artist_vectors(),
                "rec_artist_content_vectors": reader.rec_model.get_rec_artist_content_vectors(),
            }
            settings = {row[0]: row[1] for row in reader.conn.execute(
                "SELECT key,value FROM settings WHERE key IN "
                "('rec_fit_inputs','rec_content_model','rec_content_generation',"
                "'rec_content_genre_revision','rec_content_inputs','rec_artist_fit_inputs',"
                "'rec_artist_content_model','rec_mode_fit_inputs','rec_refresh_version')")}
            modes = reader.modes.list_modes()
        now = time.time()
        mode_rows = [
            (m["mode_id"], m["label"], json.dumps(m["families"]), m["centroid"].tobytes(),
             m["size"], json.dumps(m["rep_keys"]), m["first_seen"], now, m["space"])
            for m in modes]
        results = []
        lock_probe = LockProbe(store._lock)
        lock_probe.owner = threading.get_ident()
        store._lock = lock_probe

        def call(stages, name, target):
            wall, cpu = time.monotonic(), time.thread_time()
            try:
                return target()
            finally:
                stages[name] = {"wall_s": time.monotonic() - wall,
                                "cpu_s": time.thread_time() - cpu}

        def prior(stages):
            call(stages, "collaborative", lambda: store.rec_model.replace_rec_vectors(vectors["rec_vectors"]))
            call(stages, "content_pair", lambda: store.rec_model.publish_content_generation(
                vectors["rec_content_vectors"], vectors["rec_discovered_content_vectors"],
                content["model"], content["revision"], content["inputs"],
                store.settings.get_setting("rec_content_generation")))
            call(stages, "artist_collaborative", lambda: store.rec_model.replace_rec_artist_vectors(
                vectors["rec_artist_vectors"]))
            call(stages, "artist_content", lambda: store.rec_model.replace_rec_artist_content_vectors(
                vectors["rec_artist_content_vectors"]))
            call(stages, "modes", lambda: store.modes.replace_modes(modes, [], now))

        # Perturb the least significant mantissa bit of serialized float32 vectors only on
        # this disposable database. Both writers receive the exact same target bytes. This
        # isolates full/partial/no-op write costs without changing production fitting math.
        def changed(blob):
            return bytes([blob[0] ^ 1]) + blob[1:] if blob else blob

        if scenario == 'full':
            payload = {table: [(key, changed(blob)) for key, blob in rows]
                       for table, rows in vectors.items()}
        elif scenario == 'partial':
            table = next((table for table, rows in vectors.items() if rows), None)
            if table is None:
                pytest.skip('No saved model vectors')
            rows = vectors[table]
            payload = {table: [(rows[0][0], changed(rows[0][1])), *rows[1:]]}
        else:
            payload = vectors

        active_stages = {}
        original_commit = db_diagnostics.DiagnosticConnection.commit
        original_checkpoint = db_snapshot.checkpoint_wal
        original_capture = Revisions.capture
        monkeypatch.setattr(db_diagnostics.DiagnosticConnection, 'commit',
            lambda conn: call(active_stages, 'commit', lambda: original_commit(conn)))
        monkeypatch.setattr(db_snapshot, 'checkpoint_wal',
            lambda path: call(active_stages, 'checkpoint', lambda: original_checkpoint(path)))
        monkeypatch.setattr(Revisions, 'capture',
            lambda revisions: call(active_stages, 'revision_capture', lambda: original_capture(revisions)))

        def atomic(stages, optimized):
            revision = store.rec_refresh.revision()
            with store.read_snapshot() as snapshot:
                changes = call(stages, 'vector_comparison',
                               lambda: snapshot.store.rec_model.prepare_training_changes(payload))
                stages['vector_changes'] = {table: {'upserts': len(delta.upserts), 'removed': len(delta.removed)}
                                            for table, delta in changes.items()}

                def replace_all():
                    # Exact pre-#252 atomic writer, retained here as the comparison control.
                    for table, rows in payload.items():
                        column = 'artist' if table.startswith('rec_artist_') else 'identity_key'
                        store.conn.execute(f'DELETE FROM {table}')
                        store.conn.executemany(f'INSERT INTO {table}({column},vec) VALUES (?,?)', rows)

                def publish():
                    call(stages, 'vectors', lambda: store.rec_model.publish_training_generation(changes)
                         if optimized else replace_all())
                    call(stages, "settings", lambda: store.settings.publish_values(settings))
                    call(stages, "modes", lambda: store.modes.publish_generation(mode_rows, []))
                    call(stages, "acknowledge", lambda: store.rec_refresh.acknowledge(revision))
                assert call(stages, 'complete_publication', lambda: snapshot.publish(
                    publish, defer_checkpoint=optimized))

        # Alternate order so a single cold first write does not decide the result.
        for optimized in (False, True, True, False):
            active_stages = {}
            prior({})  # Restore exactly the same starting rows/settings/modes before each trial.
            lock_probe.holds.clear()
            stages = active_stages = {}
            atomic(stages, optimized)
            for table, expected in payload.items():
                column = 'artist' if table.startswith('rec_artist_') else 'identity_key'
                assert dict(store.conn.execute(f'SELECT {column},vec FROM {table}')) == dict(expected)
            if optimized and scenario == 'unchanged':
                assert stages['vector_changes'] == {}
            if optimized and scenario == 'partial':
                assert sum(delta['upserts'] for delta in stages['vector_changes'].values()) == 1
            results.append({'path': 'after_atomic' if optimized else 'before_atomic', 'stages': stages,
                            'serving_lock_holds': list(lock_probe.holds),
                            'serving_lock_max_ms': max((h['wall_s'] for h in lock_probe.holds), default=0) * 1000})

        # Isolate the cost of the newly added TEMP revision triggers. This is
        # strictly a diagnostic on the disposable connection, never a live fix.
        active_stages = {}
        triggers = [row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_temp_schema WHERE type='trigger' "
            "AND name LIKE 'snapshot_revision_%'")]
        for name in triggers:
            store.conn.execute('DROP TRIGGER temp."' + name.replace('"', '""') + '"')
        for _ in range(2):
            stages = active_stages = {}
            prior(stages)
            results.append({"path": "prior_writers_without_revision_tracking", "stages": stages})
        report = {"scenario": scenario, "vector_spaces": {name: {"rows": len(rows),
                                         "blob_mib": sum(len(blob) for _key, blob in rows) / 2**20}
                                     for name, rows in vectors.items()},
                  "modes": len(modes), "results": results}
        report_path = Path(f'/tmp/tuneconsole-publication-252-{scenario}.json')
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(report_path), **report}), flush=True)
    finally:
        store.conn.close()


if __name__ == "__main__":
    faulthandler.dump_traceback_later(45, repeat=True)
    refresh = next((arg.split('=', 1)[1] for arg in sys.argv[2:] if arg.startswith('--refresh=')), None)
    projection = next((arg.split('=', 1)[1] for arg in sys.argv[2:] if arg.startswith('--projection=')), None)
    print(json.dumps(measure(sys.argv[1], full_generation="--full-generation" in sys.argv[2:],
                             refresh=refresh, projection=projection,
                             inline_checkpoint='--inline-checkpoint' in sys.argv[2:])))
