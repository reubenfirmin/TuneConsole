"""Conflict publication preserves installed decisions and compatible writers."""
import json
from tests.enrichment_support import choose_field
from tests.metadata_provenance_support import metadata_provenance
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_metadata_owner_cutover import predecessor
from tests.core.test_metadata_conflict_preparation import seed, finish, head
from tests.independent_schema_support import independent_regular_ddl
from yt_playlist.core.migrations import regular, MigrationError
from yt_playlist.core.preparation import conflict_publication as publication, metadata_conflicts as preparation


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Conflict preparation and publication do not construct an HTTP app."""


def publish(conn, size=1):
    for _ in range(100):
        stage = publication.OWNER.step(conn, batch_size=size)
        if stage.complete:
            publication.OWNER.publish(conn, stage=stage.id)
            return stage.id
    pytest.fail('Conflict publication did not finish')


def rejection_inputs(store):
    seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    assert publication.OWNER.selected(store.conn) is None


def test_order_duplicates_unknowns_and_decision_have_exact_independent_custody(predecessor):
    conn = predecessor.conn
    identity, original = seed(predecessor)
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    preparation.JOB.install(conn)
    finish(conn)
    publish(conn)
    def authorize(action, owner, column, *rest):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and owner == 'enrichment_conflict' else sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)
    try:
        row = dict(conn.execute('SELECT * FROM '+publication.table(conn)+' WHERE id=?', (identity,)).fetchone())
        assert json.loads(row.pop('candidates')) == json.loads(original['candidates'])
        assert row == {key:value for key,value in original.items() if key != 'candidates'}
        assert predecessor.enrichment_reports.outstanding_conflicts() == 0
    finally:
        conn.set_authorizer(None)
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
    choice = conn.execute('SELECT * FROM '+preparation.RESOLUTIONS).fetchone()
    assert choice['raw_value'] == '130'
    assert not any('candidate' in name or 'observation' in name for name in choice.keys())
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with conn:
            conn.execute('UPDATE '+preparation.COMPATIBILITY+" SET resolved_value='120'")
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with conn:
            conn.execute('DELETE FROM '+preparation.CANDIDATES)


@pytest.mark.parametrize('raw', ['broken [', 'null', '{}', '[NaN]'])
def test_malformed_nonlist_bodies_remain_exact_and_do_not_invent_candidates(predecessor, raw):
    identity, original = seed(predecessor, raw, resolved=2, selected=None, field='unknown')
    conn = predecessor.conn
    preparation.JOB.install(conn)
    finish(conn)
    publish(conn)
    assert dict(conn.execute('SELECT * FROM '+publication.table(conn)+' WHERE id=?', (identity,)).fetchone()) == original
    assert conn.execute('SELECT count(*) FROM '+preparation.CANDIDATES).fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM '+preparation.RESOLUTIONS).fetchone()[0] == 0


def test_reports_reads_selected_owner_and_supported_write_withdraws_it(predecessor):
    conn = predecessor.conn
    identity, original = seed(predecessor, [{'provider':'a','value':120},{'provider':'b','value':130}], resolved=0)
    before = predecessor.enrichment_reports.unresolved_enrichment_conflicts()
    preparation.JOB.install(conn)
    finish(conn)
    old = publish(conn)
    assert predecessor.enrichment_reports.unresolved_enrichment_conflicts() == before
    predecessor.enrichment.resolve_conflict(original['track_id'], 'bpm', '140')
    assert publication.OWNER.selected(conn) is None
    assert predecessor.enrichment_reports.outstanding_conflicts() == 0
    finish(conn)
    assert publish(conn) != old
    assert conn.execute('SELECT resolved_value FROM '+publication.table(conn)).fetchone()[0] == '140'
    with conn:
        conn.execute('DELETE FROM enrichment_conflict WHERE id=?', (identity,))
    assert publication.OWNER.selected(conn) is None
    finish(conn)
    publish(conn)
    assert conn.execute('SELECT count(*) FROM '+publication.table(conn)).fetchone()[0] == 0


@pytest.mark.parametrize('damage', [
    "UPDATE prepared_metadata_conflict_candidates SET number_value=999 WHERE position=1",
    "UPDATE prepared_metadata_conflict_resolutions SET raw_value='120'",
    "UPDATE prepared_metadata_conflict_generations SET recording_id=NULL",
    "UPDATE prepared_metadata_conflict_compatibility SET field='genre'",
    'DELETE FROM prepared_metadata_conflict_heads',
])
def test_damaged_preparation_blocks_selection_without_blocking_regular_schema(predecessor, damage, independent_regular_ddl):
    conn = predecessor.conn
    rejection_inputs(predecessor)
    with conn:
        conn.execute(damage)
    with pytest.raises(MigrationError, match='differ|omits'):
        publish(conn)
    assert publication.OWNER.selected(conn) is None
    regular.migrate(conn)


def test_missing_file_recovery_and_source_change_during_verification(predecessor, monkeypatch):
    conn = predecessor.conn
    identity, original = seed(predecessor)
    preparation.JOB.install(conn)
    finish(conn)
    Path(head(conn, identity)['filename']).unlink()
    with pytest.raises(OSError):
        publish(conn)
    assert conn.execute('SELECT candidates FROM enrichment_conflict').fetchone()[0] == original['candidates']
    publication.repair(conn, identity)
    publish(conn)
    verify = type(publication.OWNER).verify
    def changed(self, connection, stage):
        proof = verify(self, connection, stage)
        with connection:
            connection.execute('UPDATE enrichment_conflict SET updated_at=20')
        return proof
    monkeypatch.setattr(type(publication.OWNER), 'verify', changed)
    with pytest.raises(MigrationError, match='changed|reconciled'):
        publish(conn)
    assert publication.OWNER.selected(conn) is None


def test_new_compatibility_capture_replays_an_existing_prepared_job(predecessor):
    conn = predecessor.conn
    seed(predecessor)
    preparation.JOB.install(conn)
    finish(conn)
    old = preparation.JOB.progress(conn)
    with conn:
        conn.execute('DROP TABLE '+preparation.COMPATIBILITY)
    preparation.JOB.install(conn)
    assert preparation.JOB.progress(conn).input_revision == old.input_revision+1
    finish(conn)
    publish(conn)
