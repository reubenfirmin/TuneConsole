"""Draft preparation preserves decisions while the installed draft keeps serving."""
from dataclasses import replace
import json

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import collection_recovery, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import road_trip_drafts as preparation
from yt_playlist.core.store import Store


def finish(conn, size=16):
    for _ in range(100):
        progress = preparation.JOB.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Road-trip draft fixture did not finish')


def head(conn, recipe):
    return conn.execute(f'SELECT g.* FROM {preparation.CURRENT} h JOIN {preparation.HEAD} g '
                        'USING(recipe_id,generation) WHERE recipe_id=?', (recipe,)).fetchone()


def recipe(store, name='Trip'):
    return store.road_trip.save_road_trip_recipe(None, name, 50, [], [], 60, 1)


def seed(store):
    identity = recipe(store)
    store.accounts.upsert_account('Current account', 'main.json', None, True)
    store.tracks.upsert_track('known', 'Song', 'Artist', None, 100)
    value = {'revision': 'held', 'picks': ['known', 'known', 'missing'], 'banned': ['excluded', 'known'],
             'building': True, 'seed': 7, 'candidate': {'title': 'Exact été prose', 'provider_result': [1, None]}}
    raw = json.dumps(value, ensure_ascii=False, indent=2)
    with store.conn:
        store.conn.execute('INSERT INTO road_trip_drafts VALUES (?,?,11)', (identity, raw))
    return identity, raw


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_exact_decisions_repetitions_unknowns_and_authority_survive_preparation(installed):
    store, _ = installed
    conn = store.conn
    identity, raw = seed(store)
    preparation.JOB.install(conn)
    assert preparation.JOB.step(conn, batch_size=2).processed == 2
    assert head(conn, identity) is None
    regular.migrate(conn)
    finish(conn)
    row = head(conn, identity)
    assert preparation.read_original(conn, row) == raw
    assert tuple(row[name] for name in ('source_revision', 'revision_kind', 'building', 'account_id', 'legacy_author_unknown', 'updated_at')) == (
        'held', 'recorded', 1, None, 1, 11)
    assert [tuple(row) for row in conn.execute(f'SELECT selection_state,position,video_id,entry_id IS NOT NULL,unresolved_reason '
        f'FROM {preparation.ITEMS} ORDER BY selection_state,position')] == [
            ('excluded', 0, 'excluded', 0, 'unadmitted_video'), ('excluded', 1, 'known', 1, None),
            ('picked', 0, 'known', 1, None), ('picked', 1, 'known', 1, None), ('picked', 2, 'missing', 0, 'unadmitted_video')]
    assert store.road_trip.get_road_trip_draft(identity) == json.loads(raw)
    assert store.road_trip.latest_road_trip_draft() == {'recipe_id': identity, 'state': json.loads(raw)}
    assert not conn.execute("SELECT 1 FROM youtube_items WHERE video_id IN ('missing','excluded')").fetchone()
    assert [row[1] for row in conn.execute('PRAGMA main.table_info(road_trip_drafts)')] == ['recipe_id', 'state', 'updated_at']
    assert files.collect_orphans(conn) == collection_recovery.collect_orphans(conn) == 0
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_explicit_frozen_upgrade_on_separate_copy_keeps_the_same_recorded_draft(installed, tmp_path):
    store, _ = installed
    identity, raw = seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    upgraded = Store(tmp_path / 'draft-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        upgraded._bind_repositories()
        assert upgraded.road_trip.get_road_trip_draft(identity) == json.loads(raw)
        assert [tuple(row) for row in upgraded.conn.execute('SELECT selection_state,position,video_id '
            'FROM road_trip_draft_entries ORDER BY selection_state,position')] == [
                tuple(row) for row in store.conn.execute(f'SELECT selection_state,position,video_id '
                    f'FROM {preparation.ITEMS} ORDER BY selection_state,position')]
        assert tuple(upgraded.conn.execute('SELECT account_id,legacy_author_unknown,revision FROM road_trip_drafts').fetchone()) == (
            None, 1, 'held')
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('value,kind', [({}, 'missing'), ({'revision': None}, 'null'),
    ({'revision': ''}, 'blank'), ({'revision': {'invalid': 'original'}}, 'invalid')])
def test_unrecorded_revisions_remain_distinct_without_generating_a_replacement(installed, value, kind):
    store, _ = installed
    identity = recipe(store)
    store.road_trip.save_road_trip_draft(identity, value, 1)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    assert head(store.conn, identity)['source_revision'] is None
    assert head(store.conn, identity)['revision_kind'] == kind
    assert store.road_trip.get_road_trip_draft(identity) == value
    assert json.loads(preparation.read_original(store.conn, head(store.conn, identity))) == value


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_long_draft_checkpoints_individual_repeated_selections(installed):
    store, _ = installed
    identity = recipe(store)
    value = {'picks': ['repeated'] * 600, 'banned': ['repeated'] * 600}
    store.road_trip.save_road_trip_draft(identity, value, 1)
    preparation.JOB.install(store.conn)
    prior = 0
    for _ in range(15):
        progress = preparation.JOB.step(store.conn, batch_size=128)
        assert 0 <= progress.processed - prior <= 128
        prior = progress.processed
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Long draft did not finish')
    assert store.conn.execute(f'SELECT count(*) FROM {preparation.ITEMS}').fetchone()[0] == 1200
    assert head(store.conn, identity)['item_count'] == 1200
    assert json.loads(preparation.read_original(store.conn, head(store.conn, identity))) == value


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_continuing_writer_and_catalog_admission_preserve_previous_generations(installed):
    store, _ = installed
    conn = store.conn
    identity, raw = seed(store)
    preparation.JOB.install(conn)
    preparation.JOB.step(conn, batch_size=2)
    replacement = {'revision': 'new', 'picks': ['later'], 'banned': []}
    store.road_trip.save_road_trip_draft(identity, replacement, 12)
    finish(conn)
    previous = head(conn, identity)
    assert json.loads(preparation.read_original(conn, previous)) == replacement
    assert not store.road_trip.replace_road_trip_draft(identity, {'revision': 'stale'}, 13, expected_revision='held')
    store.tracks.upsert_track('later', 'Later', 'Artist', None, 14)
    assert preparation.JOB.progress(conn).prepared_revision is None
    finish(conn)
    assert conn.execute(f'SELECT entry_id FROM {preparation.ITEMS} WHERE recipe_id=? AND generation=?',
        (identity, head(conn, identity)['generation'])).fetchone()[0] is not None
    assert preparation.read_original(conn, previous) == json.dumps(replacement)
    other = recipe(store, 'Other')
    with conn:
        conn.execute('UPDATE road_trip_drafts SET recipe_id=? WHERE recipe_id=?', (other, identity))
    finish(conn)
    assert head(conn, identity) is None and head(conn, other) is not None
    store.road_trip.delete_road_trip_recipe(other)
    finish(conn)
    assert head(conn, other) is None
    assert preparation.read_original(conn, previous) == json.dumps(replacement)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_file_io_fences_a_concurrent_draft_edit_without_holding_a_transaction(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    identity, _ = seed(store)
    preparation.JOB.install(conn)
    second = Store(store._db_path)
    publish = files.publish
    calls = []
    try:
        def edit(*args):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE road_trip_drafts SET state='{}' WHERE recipe_id=?", (identity,))
                calls.append(True)
            return publish(*args)
        monkeypatch.setattr(files, 'publish', edit)
        assert preparation.JOB.step(conn, batch_size=1).input_revision == 1
        assert not conn.execute(f'SELECT 1 FROM {preparation.HEAD}').fetchone()
        finish(conn)
        assert preparation.read_original(conn, head(conn, identity)) == '{}'
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interruption_and_disk_failure_keep_prior_checkpoint_for_explicit_retry(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    identity, raw = seed(store)
    preparation.JOB.install(conn)
    checkpoint = preparation.JOB.step(conn, batch_size=2)
    copy = preparation.copy
    def interrupted(*args):
        copy(*args)
        raise KeyboardInterrupt('controlled draft member interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(preparation.JOB, copy=interrupted).step(conn, batch_size=1)
    assert preparation.JOB.progress(conn) == checkpoint
    assert conn.execute(f'SELECT count(*) FROM {preparation.ITEMS}').fetchone()[0] == 1
    publish = files.publish
    def failed(*args):
        raise OSError('controlled draft disk failure')
    monkeypatch.setattr(files, 'publish', failed)
    with pytest.raises(OSError, match='controlled draft disk failure'):
        preparation.JOB.step(conn)
    assert preparation.JOB.progress(conn).status == 'failed'
    monkeypatch.setattr(files, 'publish', publish)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.JOB.step(reopened.conn)
        preparation.JOB.step(reopened.conn, retry=True)
        finish(reopened.conn)
        assert preparation.read_original(reopened.conn, head(reopened.conn, identity)) == raw
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('value', [[], {'picks': None}, {'banned': ['']}, {'picks': [3]}, {'value': float('inf')}])
def test_invalid_draft_fails_durably_without_blocking_regular_ddl(installed, value):
    store, _ = installed
    identity = recipe(store)
    store.road_trip.save_road_trip_draft(identity, value, 1)
    preparation.JOB.install(store.conn)
    with pytest.raises(ValueError):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert store.road_trip.get_road_trip_draft(identity) == value
    regular.migrate(store.conn)
