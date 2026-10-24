"""DAO suite for OverlapRepo (split out of the Store god class)."""
import sqlite3

import pytest


def test_suppress_and_unsuppress(store):
    store.overlaps.suppress_overlap("PLB", "PLA", now=1.0)        # order is normalized to (a,b)
    assert store.overlaps.get_suppressed_overlap_pairs() == {frozenset(("PLA", "PLB"))}
    assert store.overlaps.get_suppressed_overlaps() == [("PLA", "PLB", 1.0)]
    store.overlaps.unsuppress_overlap("PLA", "PLB")
    assert store.overlaps.get_suppressed_overlap_pairs() == set()


def test_ignore_playlist(store):
    store.overlaps.ignore_overlap_playlist("PLX", now=2.0)
    assert store.overlaps.get_overlap_ignored() == {"PLX"}
    store.overlaps.unignore_overlap_playlist("PLX")
    assert store.overlaps.get_overlap_ignored() == set()


def test_kept_pair_is_unordered(store):
    store.overlaps.keep_overlap_pair("PLB", "PLA", now=2.0)
    assert store.overlaps.get_overlap_kept_pairs() == {frozenset(("PLA", "PLB"))}


@pytest.mark.parametrize('count', [4, 40])
def test_bulk_suppression_resolves_each_reference_once_and_commits_once(store, count):
    store.overlaps.suppress_overlap('ROOT', 'PL0', now=1)
    pairs = [[f'PL{i}', 'ROOT'] for i in range(count)]
    pairs += [pair[::-1] for pair in pairs] * 3
    pairs += [[], ['one'], 'junk', [None, 'ROOT'], [1, 'ROOT'], [{}, 'ROOT']]
    queries = []
    store.conn.set_trace_callback(queries.append)
    try:
        store.overlaps.suppress_overlaps(pairs, now=2)
    finally:
        store.conn.set_trace_callback(None)
    assert sum(q == 'BEGIN ' or q == 'BEGIN' for q in queries) == 1
    assert queries.count('COMMIT') == 1
    assert sum(q.startswith('SELECT 1 FROM playlist_scopes WHERE ref=') for q in queries) == count + 1
    assert set(store.overlaps.get_suppressed_overlaps()) == {
        (f'PL{i}', 'ROOT', 1 if i == 0 else 2) for i in range(count)}


def test_bulk_suppression_rolls_back_pairs_and_new_scopes_on_failure(store):
    store.overlaps.suppress_overlap('KEEP', 'STAY', now=1)
    store.conn.execute("CREATE TEMP TRIGGER reject_test_pair BEFORE INSERT ON suppressed_overlaps "
                       "WHEN NEW.b='PLZ' BEGIN SELECT RAISE(ABORT,'rejected pair'); END")
    with pytest.raises(sqlite3.IntegrityError, match='rejected pair'):
        store.overlaps.suppress_overlaps([['PLA', 'PLB'], ['PLA', 'PLZ']], now=2)
    assert store.overlaps.get_suppressed_overlaps() == [('KEEP', 'STAY', 1)]
    assert {row[0] for row in store.conn.execute('SELECT ref FROM playlist_scopes')} == {'KEEP', 'STAY'}


def test_bulk_suppression_normalizes_account_references_before_deduplication(store):
    owner = store.identities.upsert_identity('main', 'cred', None, True)
    pid = store.playlists.upsert_playlist(owner, 'LM', 'Liked Music', 0, '', 1)
    ref = store.playlists.get_playlist(pid).scope_ref
    store.overlaps.suppress_overlaps([['LM', 'OTHER'], ['OTHER', ref]], now=2)
    assert store.overlaps.get_suppressed_overlaps() == [(ref, 'OTHER', 2)]
