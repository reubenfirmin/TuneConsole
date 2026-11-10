"""Evidence roots retain public alias meaning rather than content coincidence."""
import sqlite3

import pytest

from tests.core.test_compact_groups_migration import predecessor, apply
from yt_playlist.core import group_evidence


def context(generation='first', version='first-leaf'):
    return {'group_generation': generation, 'musical_groups': [{'version': version, 'group_id': 'leaf'}]}


def test_offer_and_feedback_retain_their_exact_aliases_even_when_content_is_shared(predecessor):
    conn = predecessor
    apply(conn)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        assert group_evidence.retain(conn, 'offer', 'first-menu', context(), 10)
        assert group_evidence.retain(conn, 'response', 'first-response', context(), 11)
        assert group_evidence.retain(conn, 'offer', 'second-menu', context('second', 'second-leaf'), 12)
        assert not group_evidence.retain(conn, 'offer', 'first-menu', context(), 15)
    rows = [tuple(row) for row in conn.execute('SELECT kind,external_key,external_version,group_version_id '
                                               'FROM group_evidence_versions ORDER BY kind,external_key')]
    assert [row[:3] for row in rows] == [('offer', 'first-menu', 'first-leaf'),
                                       ('offer', 'second-menu', 'second-leaf'),
                                       ('response', 'first-response', 'first-leaf')]
    assert len({row[3] for row in rows}) == 1
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.release(conn, 'offer', 'first-menu')
    assert conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='response'").fetchone()
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_same_content_does_not_prove_that_an_alias_occurred_in_the_served_generation(predecessor):
    conn = predecessor
    apply(conn)
    with pytest.raises(ValueError, match='wrong_generation'), conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'offer', 'bad-menu', context('second', 'first-leaf'), 10)
    assert not conn.execute('SELECT 1 FROM group_evidence_owners').fetchone()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'offer', 'historical-menu', context('second', 'first-leaf'), 10, strict=False)
    assert [tuple(row) for row in conn.execute('SELECT reference_kind,raw_key,reason FROM unresolved_group_evidence')] == [
        ('version', 'first-leaf', 'wrong_generation')]


def test_historical_unknown_reference_is_not_reinterpreted_when_an_alias_appears_later(predecessor):
    conn = predecessor
    apply(conn)
    raw = {'groups': [{'definition_version': 'previously-unknown'}]}
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'offer', 'unknown-menu', raw, 10, strict=False)
        version_id, generation_id = conn.execute("SELECT group_version_id,first_generation_id FROM group_version_aliases "
                                                 "WHERE external_version='first-leaf'").fetchone()
        conn.execute('INSERT INTO group_version_aliases VALUES (?,?,?)',
                     ('previously-unknown', version_id, generation_id))
    with pytest.raises(ValueError, match='historically unresolved'), conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'offer', 'unknown-menu', raw, 11)
    assert not conn.execute("SELECT 1 FROM group_evidence_versions WHERE external_key='unknown-menu'").fetchone()
    assert conn.execute("SELECT raw_key FROM unresolved_group_evidence WHERE external_key='unknown-menu'").fetchone()[0] == 'previously-unknown'


def test_reference_ownership_and_immutable_owner_identity_are_enforced(predecessor):
    conn = predecessor
    apply(conn)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'comparison', 'comparison-1', context(), 10)
    with pytest.raises(ValueError, match='immutable references'), conn:
        conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(conn, 'comparison', 'comparison-1', context('second', 'second-leaf'), 10)
    with pytest.raises(sqlite3.IntegrityError), conn:
        parent_id = conn.execute("SELECT group_version_id FROM group_version_aliases WHERE external_version='first-parent'").fetchone()[0]
        conn.execute('INSERT INTO group_evidence_versions VALUES (?,?,?,?)',
                     ('comparison', 'comparison-1', 'second-leaf', parent_id))
    plan = ' '.join(row[3] for row in conn.execute('EXPLAIN QUERY PLAN SELECT kind,external_key FROM '
                                                  'group_evidence_versions WHERE group_version_id=?', (parent_id,)))
    assert 'ix_group_evidence_version' in plan and 'SCAN ' not in plan


def test_evidence_owner_cannot_be_published_without_the_callers_transaction(predecessor):
    conn = predecessor
    apply(conn)
    with pytest.raises(ValueError, match='outer transaction'):
        group_evidence.retain(conn, 'offer', 'unguarded', context(), 10)
