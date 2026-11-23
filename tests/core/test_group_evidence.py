"""Evidence roots retain public alias meaning rather than content coincidence."""
import sqlite3

import pytest

from tests.group_history_support import predecessor
from tests.repos.test_compact_group_publication import third_generation
from yt_playlist.core import group_evidence


def context(generation='first', version='first-leaf'):
    return {'group_generation': generation, 'musical_groups': [{'version': version, 'group_id': 'leaf'}]}


def test_offer_and_feedback_retain_their_exact_aliases_even_when_content_is_shared(predecessor):
    store, _ = predecessor
    conn = store.conn
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        assert group_evidence.retain(conn, 'offer', 'first-menu', context(), 10)
        assert group_evidence.retain(conn, 'response', 'first-response', context(), 11)
        assert group_evidence.retain(conn, 'offer', 'second-menu', context('second', 'second-leaf'), 12)
        assert not group_evidence.retain(conn, 'offer', 'first-menu', context(), 15)
    rows = [tuple(row) for row in conn.execute('SELECT kind,external_key,reference_key,group_version_id '
        f"FROM {group_evidence.REFERENCES} WHERE reference_kind='resolved_version' ORDER BY kind,external_key")]
    assert [row[:3] for row in rows] == [('offer', 'first-menu', 'first-leaf'),
                                       ('offer', 'second-menu', 'second-leaf'),
                                       ('response', 'first-response', 'first-leaf')]
    assert len({row[3] for row in rows}) == 2
    first_version = rows[0][3]
    assert conn.execute('SELECT evidence_holds FROM group_versions WHERE id=?', (first_version,)).fetchone()[0] == 2
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.release(conn, 'offer', 'first-menu')
    assert conn.execute("SELECT 1 FROM group_evidence_contexts WHERE kind='response'").fetchone()
    assert conn.execute('SELECT evidence_holds FROM group_versions WHERE id=?', (first_version,)).fetchone()[0] == 1
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_same_content_does_not_prove_that_an_alias_occurred_in_the_served_generation(predecessor):
    store, _ = predecessor
    conn = store.conn
    with pytest.raises(ValueError, match='wrong_generation'), conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'offer', 'bad-menu', context('second', 'first-leaf'), 10)
    assert not conn.execute('SELECT 1 FROM group_evidence_contexts').fetchone()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'offer', 'historical-menu', context('second', 'first-leaf'), 10, strict=False)
    assert [tuple(row) for row in conn.execute('SELECT reference_kind,reference_key,reason '
                                               f'FROM {group_evidence.REFERENCES} WHERE reason IS NOT NULL')] == [
        ('version', 'first-leaf', 'wrong_generation')]


def test_historical_unknown_reference_is_not_reinterpreted_when_an_alias_appears_later(predecessor):
    store, prepared = predecessor
    conn = store.conn
    raw = {'groups': [{'definition_version': 'previously-unknown'}]}
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'offer', 'unknown-menu', raw, 10, strict=False)
    next_generation = third_generation(prepared)
    next(group for group in next_generation['groups'] if group['group_id'] == 'leaf')['version'] = 'previously-unknown'
    store.taste_groups.publish(next_generation)
    assert store.taste_groups.definition('previously-unknown')
    with pytest.raises(ValueError, match='historically unresolved'), conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'offer', 'unknown-menu', raw, 11)
    assert not conn.execute(f"SELECT 1 FROM {group_evidence.REFERENCES} WHERE external_key='unknown-menu' "
                            "AND reference_kind='resolved_version'").fetchone()
    assert conn.execute(f"SELECT reference_key FROM {group_evidence.REFERENCES} WHERE external_key='unknown-menu' "
                        'AND reason IS NOT NULL').fetchone()[0] == 'previously-unknown'


def test_reference_ownership_and_immutable_owner_identity_are_enforced(predecessor):
    store, _ = predecessor
    conn = store.conn
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'comparison', 'comparison-1', context(), 10)
    with pytest.raises(ValueError, match='immutable references'), conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'comparison', 'comparison-1', context('second', 'second-leaf'), 10)
    with pytest.raises(sqlite3.IntegrityError, match='missing generation or version'), conn:
        parent_id = conn.execute("SELECT id FROM group_versions WHERE external_version='first-parent'").fetchone()[0]
        conn.execute('INSERT INTO settings(key,value) VALUES (?,json_array(?,10,NULL,NULL,NULL,'
                     "json_array(json_array('second-leaf','resolved_version',?,NULL))))",
                     (group_evidence.setting_key('comparison', 'invalid'), 'a' * 64, parent_id))
    identity = group_evidence.entry(conn, 'comparison', 'comparison-1')
    with pytest.raises(sqlite3.IntegrityError, match='retains this identity'), conn:
        conn.execute('DELETE FROM group_versions WHERE id=?', (identity[5][0][2],))
    with pytest.raises(sqlite3.IntegrityError, match='retains this identity'), conn:
        conn.execute('DELETE FROM group_generations WHERE id=?', (identity[4],))
    plan = ' '.join(row[3] for row in conn.execute('EXPLAIN QUERY PLAN SELECT evidence_holds FROM '
                                                  'group_versions WHERE id=?', (parent_id,)))
    assert 'SEARCH group_versions USING INTEGER PRIMARY KEY' in plan


def test_evidence_owner_cannot_be_published_without_the_callers_transaction(predecessor):
    store, _ = predecessor
    conn = store.conn
    with pytest.raises(ValueError, match='outer transaction'):
        group_evidence.retain(conn, 'offer', 'unguarded', context(), 10)
