"""Explicit #228/#248 before/after benchmark, serial under pytest's machine lock.

The reference is the reviewed, local Git revision, never the live database.
Run: uv run pytest -s tests/bench_cleanup_analysis.py
"""
from collections import Counter
import itertools
import json
from pathlib import Path
import subprocess
import time
import tracemalloc
import types

import pytest

from yt_playlist.core import db_diagnostics
from yt_playlist.core.store import Store
from yt_playlist.library import analysis


def _legacy_page(module, store):
    ci = store.overlaps.get_cleanup_ignored()
    ignored_sigs = store.overlaps.get_ignored_merge_sigs()
    dupes = module.find_dupes(store)
    groups = module.find_identical_groups(store, ignored_sigs=ignored_sigs)
    empties = module.find_empty_playlists(store, ignored=ci.get('empty'))
    tiny = module.find_tiny_playlists(store, ignored=ci.get('tiny'))
    exact = {p.id for g in groups for p in g.playlists}
    near = module.find_near_duplicate_groups(store, exclude_playlist_ids=exact, ignored_sigs=ignored_sigs)
    prefs = dict(suppressed=store.overlaps.get_suppressed_overlap_pairs(),
                 ignored_ytm=store.overlaps.get_overlap_ignored(), kept=store.overlaps.get_overlap_kept_pairs())
    near_pairs = module.near_duplicate_pairs(store, near, **prefs)
    dupe_ids = {p.id for d in dupes for p in (d.playlist_a, d.playlist_b)}
    overlaps = module.find_overlaps(store, exclude_playlist_ids=dupe_ids, **prefs)
    kinds = {p.scope_ref: store.playlists.playlist_kind(p.id) for p in store.playlists.get_playlists()}
    summary = module.cleanup_summary(store)
    return _shape(groups, near_pairs, overlaps, empties, tiny, kinds, summary)


def _shape(groups, near, overlaps, empties, tiny, kinds, summary):
    def pairs(values):
        return [(p.playlist_a.id, p.playlist_b.id, sorted(p.shared), p.count_a, p.count_b) for p in values]
    return dict(groups=[([p.id for p in g.playlists], g.track_count) for g in groups],
                near=pairs(near), overlaps=pairs(overlaps), empties=[p.id for p in empties],
                tiny=[p.id for p in tiny], kinds=kinds, summary=[p.id for p in summary.playlists])


def _current_page(store):
    result = analysis.analyze_cleanup(store)
    return _shape(result.groups, result.near_pairs, result.overlaps, result.empties, result.tiny,
                  result.analysis.memberships.kinds, result.summary())


def _seed(store, size, sparse=False):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    tids = [store.tracks.upsert_track(f'v{i}', f'Song {i}', 'Artist', None, 100)
            for i in range(size * 12 if sparse else 80)]
    for i in range(size):
        role = i % 12
        members = (tids[i * 12:(i + 1) * 12] if sparse else
                   tids[:12] if role < 2 else tids[:10] + [tids[10 + role]] if role < 5 else
                   [] if role == 5 else tids[20:22] if role == 6 else tids[role:role + 12])
        pid = store.playlists.upsert_playlist(iid, f'P{i}', f'Playlist {i}', len(members), '', 1)
        store.playlists.set_playlist_tracks(pid, members)
    for name in ('LM', 'SE', 'Generated'):
        pid = store.playlists.upsert_playlist(iid, name, name, 12, '', 1)
        store.playlists.set_playlist_tracks(pid, tids[:12])
        if name == 'Generated':
            store.playlists.mark_generated_playlist(name)
    store.overlaps.ignore_cleanup('P5', 'empty', 1)
    store.overlaps.ignore_cleanup('P6', 'tiny', 1)
    store.overlaps.ignore_overlap_playlist('P7', 1)
    store.overlaps.keep_overlap_pair('P7', 'P8', 1)
    store.overlaps.suppress_overlap('P8', 'P9', 1)
    store.overlaps.suppress_overlap('P2', 'P3', 1)
    exact = [f'P{i}' for i in range(size) if i % 12 < 2]
    store.overlaps.ignore_merge(analysis.merge_signature(exact), exact, 1)


@pytest.mark.parametrize('sparse', [False, True])
def test_cleanup_before_after(tmp_path, monkeypatch, sparse):
    reference = subprocess.run(['git', 'show', '8f44072:src/yt_playlist/library/analysis.py'],
                               capture_output=True, text=True, check=True).stdout
    legacy = types.ModuleType('cleanup_228_reference')
    exec(compile(reference, 'cleanup_228_reference', 'exec'), legacy.__dict__)
    report = []
    for size in (20, 80, 200):
        store = Store(tmp_path / f'cleanup-{size}.db')
        store.init_schema()
        try:
            _seed(store, size, sparse)
            measured = {}
            results = {}
            for name, module, target in (('before', legacy, lambda: _legacy_page(legacy, store)),
                                          ('after', analysis, lambda: _current_page(store))):
                counts = Counter()
                original = db_diagnostics.DiagnosticCursor._call

                def sql(cursor, stage, callback, *args):
                    if stage == 'execute':
                        statement = args[0].lower()
                        counts['sql_statements'] += 1
                        if statement.startswith('select') and 'from playlist_tracks' in statement:
                            counts['membership_queries'] += 1
                    return original(cursor, stage, callback, *args)

                def comparisons(*args):
                    for pair in itertools.combinations(*args):
                        counts['pair_comparisons'] += 1
                        yield pair

                original_candidates = analysis.CleanupAnalysis._candidate_pairs

                def candidates(*args):
                    for pair in original_candidates(*args):
                        counts['pair_comparisons'] += 1
                        yield pair

                with monkeypatch.context() as patch:
                    if name == 'before':
                        patch.setattr(module, 'combinations', comparisons)
                    else:
                        patch.setattr(analysis.CleanupAnalysis, '_candidate_pairs', candidates)
                    patch.setattr(db_diagnostics.DiagnosticCursor, '_call', sql)
                    tracemalloc.start()
                    started = time.perf_counter()
                    try:
                        results[name] = target()
                        measured[name] = dict(counts, seconds=round(time.perf_counter() - started, 6),
                                              peak_bytes=tracemalloc.get_traced_memory()[1])
                    finally:
                        tracemalloc.stop()
            assert results['before'] == results['after']
            assert measured['after']['membership_queries'] == 1
            assert measured['after'].get('pair_comparisons', 0) <= size * (size - 1) // 2
            if sparse:
                assert measured['after'].get('pair_comparisons', 0) == 0
            report.append(dict(playlists=size, results_equal=True, **measured))
        finally:
            store.conn.close()
    output = Path(f'/tmp/tuneconsole-cleanup-248-{"sparse" if sparse else "dense"}.json')
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)
