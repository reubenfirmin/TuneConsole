"""Taste publication proves the complete retained document before selection."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_taste_control_preparation import finish, members, seed
from tests.publication_fixture_support import installed
from tests.store_fixture_support import numbered_store as unadmitted_installed
from tests.store_fixture_support import (
    mock_regular_numbered_input, native_sql_calls, preparation_ddl_connection,
    publication_consumer_reads, reuse_reporting_reader, without_query_timing,
)
from yt_playlist.core import collection_recovery, experiment_decision_state, recommendation_outputs as files, schema_inspection, taste_control_state
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import taste_controls as preparation, taste_control_publication as publication
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('without_query_timing', 'publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Taste SQL and file publication do not start the HTTP lifecycle."""


def prepare(conn, owner):
    owner.worker.install(conn)
    return finish(owner.worker, conn).prepared_revision


@pytest.mark.parametrize('installed', [79, 93, 95], indirect=True)
def test_preopened_and_reopened_readers_use_exact_controls_and_keep_unknowns(installed, monkeypatch):
    store, _ = installed
    values, originals = seed(store)
    before = {table:list(map(tuple, store.conn.execute('SELECT * FROM '+table)))
              for table in ('recordings', 'genre_catalog')}
    # Exercise fresh serving connections to the admitted input, rather than
    # repeating whole-library startup for each publication reader.
    with monkeypatch.context() as patch, reuse_reporting_reader(store, patch) as other:
        for owner in publication.OWNERS.values():
            owner.publish(store.conn, revision=prepare(store.conn, owner))
            assert owner.selected(other.conn) == 0
        assert {key:other.settings.get_setting(key) for key in originals} == originals
        assert not taste_control_state.available(other.conn)
        assert not experiment_decision_state.available(other.conn)
        regular.validate(other.conn)

    with monkeypatch.context() as patch, reuse_reporting_reader(store, patch) as reopened:
        assert {key:reopened.settings.get_setting(key) for key in originals} == originals
        assert before == {table:list(map(tuple, reopened.conn.execute('SELECT * FROM '+table))) for table in before}
        assert members(reopened.conn, preparation.BASELINE, 'entries')[1]['unresolved_key'] == 'unknown:1'



@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('raw', ['absent', None, '', 'null'])
def test_absent_sql_null_blank_and_json_null_remain_distinct(installed, raw):
    store, _ = installed
    for name, owner in publication.OWNERS.items():
        # JSON null is not a valid experiment-decision array.
        if name == 'decisions' and raw == 'null':
            continue
        if raw != 'absent':
            with store.conn:
                store.conn.execute('INSERT INTO settings(key,value) VALUES (?,?)', (owner.source, raw))
        owner.publish(store.conn, revision=prepare(store.conn, owner))
        assert store.settings.get_setting(owner.source, 'missing') == ('missing' if raw == 'absent' else raw)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['weight', 'extra-weight', 'entry', 'decision', 'metadata', 'original', 'plan', 'recovery'])
def test_corrupt_members_metadata_or_files_never_select_a_control(installed, failure):
    store, _ = installed
    _, originals = seed(store)
    name = 'baseline' if failure == 'entry' else 'decisions' if failure == 'decision' else 'checkpoint'
    owner = publication.OWNERS[name]
    revision = prepare(store.conn, owner)
    header = preparation.head(store.conn, owner.source)
    with store.conn:
        if failure == 'weight':
            store.conn.execute('UPDATE prepared_taste_checkpoint_weights SET weight=2 WHERE position=0')
        elif failure == 'extra-weight':
            store.conn.execute("INSERT INTO prepared_taste_checkpoint_weights SELECT source,preparation_revision,2,'different',"
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',axis_ref,kind,genre_key,family_key,value,"
                'resolution_reason,weight,updated_at FROM prepared_taste_checkpoint_weights WHERE position=1')
        elif failure == 'entry':
            store.conn.execute("UPDATE prepared_taste_comparison_entries SET unresolved_key='different' WHERE position=1")
        elif failure == 'decision':
            store.conn.execute("UPDATE prepared_experiment_decisions_decisions SET basis='different' WHERE position=0")
        elif failure == 'metadata':
            store.conn.execute('UPDATE prepared_taste_checkpoint_generations SET saved_at=11')
        else:
            filename = (header['plan_filename'] if failure == 'plan' else header['recovery_filename'] if failure == 'recovery'
                        else members(store.conn, owner.source, 'parts')[0]['filename'])
            Path(filename).write_bytes(b'corrupt')
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) is None
    assert store.settings.get_setting(owner.source) == originals[owner.source]


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_file_verification_is_outside_transactions_and_fences_concurrent_source_changes(installed, monkeypatch):
    store, _ = installed
    _, originals = seed(store)
    owner = publication.OWNERS['checkpoint']
    revision = prepare(store.conn, owner)
    original_read, changed = collection_recovery.read, False
    def race(*args, **kwargs):
        nonlocal changed
        assert not store.conn.in_transaction
        if not changed:
            changed = True
            other = sqlite3.connect(store._db_path, timeout=.01)
            try:
                with other:
                    other.execute('UPDATE settings SET value=? WHERE key=?', ('null', owner.source))
            finally:
                other.close()
        return original_read(*args, **kwargs)
    monkeypatch.setattr(collection_recovery, 'read', race)
    with pytest.raises(MigrationError, match='reconciled|changed'):
        owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) is None
    assert store.settings.get_setting(owner.source) == 'null'
    monkeypatch.undo()
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert owner.selected(store.conn) is not None
    assert store.settings.get_setting(owner.source) == 'null'


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_publication_rechecks_custody_and_rolls_back_interruption(installed, monkeypatch):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['baseline']
    revision = prepare(store.conn, owner)
    original_verify = owner.verify
    def race(self, conn, rev):
        proof = original_verify(conn, rev)
        with conn:
            conn.execute('UPDATE prepared_taste_comparison_entries SET score=score+1 WHERE position=0')
        return proof
    monkeypatch.setattr(type(owner), 'verify', race)
    with pytest.raises(MigrationError, match='changed after'):
        owner.publish(store.conn, revision=revision)
    monkeypatch.undo()
    with store.conn:
        store.conn.execute('UPDATE prepared_taste_comparison_entries SET score=.2 WHERE position=0')
        store.conn.execute('CREATE TRIGGER controlled_publication_interrupt BEFORE INSERT ON '+owner.tables[1]+
                           " BEGIN SELECT RAISE(ABORT,'controlled interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled interruption'):
        owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) is None
    assert store.conn.execute('SELECT count(*) FROM '+owner.tables[0]).fetchone()[0] == 0
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_publication_interrupt')
    owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) == revision


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_independent_writes_withdraw_selection_and_preserve_pinned_snapshots(installed):
    store, _ = installed
    _, originals = seed(store)
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    checkpoint = publication.OWNERS['checkpoint']
    baseline = publication.OWNERS['baseline']
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting(checkpoint.source) == originals[checkpoint.source]
        store.settings.set_setting(checkpoint.source, 'null')
        assert not snapshot.publish(lambda:None)
    assert checkpoint.selected(store.conn) is None
    assert baseline.selected(store.conn) is not None
    checkpoint.publish(store.conn, revision=prepare(store.conn, checkpoint))
    assert checkpoint.value(store.conn, 0) == originals[checkpoint.source]
    with store.conn:
        store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('unknown:1')")
    assert baseline.selected(store.conn) is None
    assert checkpoint.selected(store.conn) is not None
    baseline.publish(store.conn, revision=prepare(store.conn, baseline))
    assert members(store.conn, baseline.source, 'entries')[1]['recording_id'] is not None


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('file_kind', ['original', 'plan', 'recovery'])
def test_missing_retained_files_require_explicit_retry_without_mutating_published_rows(
        installed, file_kind, preparation_ddl_connection):
    store, _ = installed
    conn = preparation_ddl_connection
    _, originals = seed(store)
    owner = publication.OWNERS['baseline']
    revision = prepare(conn, owner)
    owner.publish(conn, revision=revision)
    header = preparation.head(conn, owner.source)
    filename = (members(conn, owner.source, 'parts')[0]['filename'] if file_kind == 'original'
                else header[file_kind+'_filename'])
    Path(filename).unlink()
    with pytest.raises(FileNotFoundError):
        owner.publish(conn, revision=revision)
    assert owner.worker.progress(conn).status == 'prepared'
    regular.migrate(conn)
    owner.worker.step(conn, retry=True)
    owner.publish(conn, revision=revision)
    assert store.settings.get_setting(owner.source) == originals[owner.source]
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with conn:
            conn.execute('DELETE FROM prepared_taste_comparison_entries')


@pytest.mark.parametrize('unadmitted_installed', [85], indirect=True)
@pytest.mark.usefixtures('native_sql_calls')
def test_new_optional_owners_withdraw_old_selection_without_blocking_regular_ddl(unadmitted_installed, tmp_path, monkeypatch):
    # Both connections perform real DDL/publication without Store snapshots.
    store, _ = unadmitted_installed
    assert list(map(tuple, store.conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS[:85]]
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    _, originals = seed(store)
    owner = publication.OWNERS['checkpoint']
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    upgraded = Store(tmp_path/'taste-optional-publication.db')
    with closing(upgraded.conn), schema_inspection.reuse_tables(upgraded.conn):
        upgraded.conn.execute('PRAGMA synchronous=OFF')
        store.conn.backup(upgraded.conn)
        upgraded.conn.execute('PRAGMA journal_mode=WAL')
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:88])
        assert owner.selected(upgraded.conn) is None
        with monkeypatch.context() as patch:
            mock_regular_numbered_input(patch, upgraded.conn)
            regular.migrate(upgraded.conn)
        owner.publish(upgraded.conn, revision=prepare(upgraded.conn, owner))
        from yt_playlist.core import taste_axes
        with upgraded.conn:
            axis = taste_axes.identity(upgraded.conn, 'genre:An unknown original', create=True)
        assert owner.selected(upgraded.conn) is None
        owner.publish(upgraded.conn, revision=prepare(upgraded.conn, owner))
        assert members(upgraded.conn, owner.source, 'weights')[0]['axis_ref'] == axis
        assert owner.value(upgraded.conn, 0) == originals[owner.source]


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_large_ordered_baseline_and_numeric_affinity_preserve_exact_original(installed):
    store, _ = installed
    values, _ = seed(store, count=2048)
    values[preparation.CHECKPOINT]['saved_at'] = 9007199254740993
    values[preparation.CHECKPOINT]['weights'][0]['weight'] = 9007199254740993
    values[preparation.BASELINE][0]['score'] = 9007199254740993
    values[preparation.DECISIONS][0]['at'] = 9007199254740993
    for owner in publication.OWNERS.values():
        raw = json.dumps(values[owner.source])
        store.settings.set_setting(owner.source, raw)
        owner.publish(store.conn, revision=prepare(store.conn, owner))
        assert store.settings.get_setting(owner.source) == raw


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_compatible_checkpoint_save_restore_and_explicit_retirement_keep_working(installed, monkeypatch):
    from yt_playlist.rec import experiments, scorecard
    store, _ = installed
    seed(store)
    store.rec_model.set_weight('lane:neighbourhood', 1.5, now=12)
    store.settings.set_setting('rec_param:breadth_bias', '.7')
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    checkpoint = publication.OWNERS['checkpoint']
    store.rec_model.save_taste_settings(checkpoint.source, ['rec_param:breadth_bias'], 15, replace=True)
    assert checkpoint.selected(store.conn) is None
    checkpoint.publish(store.conn, revision=prepare(store.conn, checkpoint))
    store.rec_model.set_weight('lane:neighbourhood', 2, now=20)
    store.settings.set_setting('rec_param:breadth_bias', '.1')
    assert store.rec_model.restore_taste_settings(checkpoint.source, ['rec_param:breadth_bias'])
    assert store.rec_model.weight_records()['lane:neighbourhood']['weight'] == 1.5
    assert store.settings.get_setting('rec_param:breadth_bias') == '.7'
    monkeypatch.setattr(scorecard, 'experiment_report', lambda *args:{'live':{}, 'min_clusters_per_arm':100})
    result = experiments.decide(store, 'retire', 'all', 'Explicit retirement after inspection', 25)
    assert result['decision']['at'] == 25
    owner = publication.OWNERS['decisions']
    assert owner.selected(store.conn) is None
    assert len(experiments.decisions(store)) == 3
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert experiments.last_decision(store)['reason'] == 'Explicit retirement after inspection'
