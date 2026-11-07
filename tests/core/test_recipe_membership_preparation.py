"""Recipe preparation batches members, fences edits and retains ordered originals."""
import json
from pathlib import Path

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import recipe_memberships as preparation
from yt_playlist.core.store import Store


LISTS = {'artists': ['Known artist', 'Unadmitted artist', 'Known artist'],
         'genres': ['Known', 'Unadmitted genre', 'Known'], 'blacklist_genres': ['Known']}


def seed(store, lists=LISTS):
    conn = store.conn
    with conn:
        conn.execute("INSERT INTO artists(id,name,provisional) VALUES (111,'Known artist',0)")
        conn.execute("INSERT INTO artist_aliases VALUES (111,'Known artist','fixture')")
        conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('known','Known','custom')")
        conn.execute('INSERT INTO road_trip_recipes '
            '(id,name,own_pct,artists,genres,blacklist_genres,target_minutes,created_at,updated_at) '
            'VALUES (1,\'Saved trip\',50,?,?,?,90,10,10)',
            tuple(json.dumps(lists[name], indent=2, ensure_ascii=False) for name in preparation.FIELDS))
    return {name: conn.execute('SELECT '+name+' FROM road_trip_recipes WHERE id=1').fetchone()[0]
            for name in preparation.FIELDS}


def finish(conn):
    for _ in range(100):
        progress = preparation.JOB.step(conn, batch_size=2)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Recipe preparation did not finish')


def head(conn, recipe_id=1):
    return conn.execute(f'SELECT g.* FROM {preparation.HEAD} g JOIN prepared_recipe_heads h '
                        'USING(recipe_id,generation) WHERE recipe_id=?', (recipe_id,)).fetchone()


@pytest.mark.parametrize('installed', [79, 88], indirect=True)
def test_member_batches_keep_repetitions_unresolved_labels_and_conflicts(installed):
    store, _ = installed
    original = seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    first = preparation.JOB.step(conn, batch_size=2)
    assert first.processed == 2 and first.prepared_revision is None
    assert conn.execute(f'SELECT count(*) FROM {preparation.ITEMS}').fetchone()[0] == 2
    assert head(conn) is None
    regular.migrate(conn)
    finish(conn)
    captured = head(conn)
    assert captured['members_count'] == 7
    assert preparation.read_original(captured) == original
    members = list(map(tuple, conn.execute(f'SELECT role,position,raw_label,artist_id,genre_key,unresolved_reason,'
        f'duplicate_position FROM {preparation.ITEMS} WHERE recipe_id=1 AND generation=? ORDER BY role,position',
        (captured['generation'],))))
    assert members == [
        ('artist', 0, 'Known artist', 111, None, None, None),
        ('artist', 1, 'Unadmitted artist', None, None, 'unknown_or_ambiguous_artist', None),
        ('artist', 2, 'Known artist', 111, None, None, 0),
        ('exclude', 0, 'Known', None, 'known', None, None),
        ('include', 0, 'Known', None, 'known', None, None),
        ('include', 1, 'Unadmitted genre', None, None, 'unknown_genre', None),
        ('include', 2, 'Known', None, 'known', None, 0)]
    assert {name: conn.execute('SELECT '+name+' FROM road_trip_recipes WHERE id=1').fetchone()[0]
            for name in preparation.FIELDS} == original
    assert not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recipe_artists'").fetchone()
    # Catalog admission changes the next prepared generation without rewriting
    # captured historical labels or their previous unresolved interpretation.
    with conn:
        conn.execute("INSERT INTO artist_aliases VALUES (111,'Unadmitted artist','fixture')")
        conn.execute("INSERT INTO genre_alias VALUES ('unadmitted genre','Unadmitted genre','known','custom')")
    assert preparation.JOB.progress(conn).prepared_revision is None
    finish(conn)
    assert head(conn)['generation'] > captured['generation']
    assert conn.execute(f'SELECT artist_id FROM {preparation.ITEMS} WHERE recipe_id=1 AND generation=? '
        "AND role='artist' AND position=1", (head(conn)['generation'],)).fetchone()[0] == 111
    assert preparation.read_original(captured) == original
    assert conn.execute(f'SELECT unresolved_reason FROM {preparation.ITEMS} WHERE recipe_id=1 AND generation=? '
        "AND role='artist' AND position=1", (captured['generation'],)).fetchone()[0] == 'unknown_or_ambiguous_artist'


@pytest.mark.parametrize('installed', [88], indirect=True)
def test_source_replacement_key_rename_and_deletion_reconcile_without_losing_archives(installed):
    store, _ = installed
    original = seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    preparation.JOB.step(conn, batch_size=2)
    with conn:
        conn.execute("UPDATE road_trip_recipes SET artists='[]',genres='[\"Known\"]',blacklist_genres='[]' WHERE id=1")
    finish(conn)
    captured = head(conn)
    assert captured['members_count'] == 1
    assert conn.execute(f'SELECT count(*) FROM {preparation.ITEMS} WHERE recipe_id=1 AND generation=?',
                        (captured['generation'],)).fetchone()[0] == 1
    initial = conn.execute(f'SELECT * FROM {preparation.HEAD} WHERE recipe_id=1 AND generation=0').fetchone()
    assert preparation.read_original(initial) == original
    # The new key sorts before the fixed initial checkpoint.
    with conn:
        conn.execute('UPDATE road_trip_recipes SET id=0 WHERE id=1')
    finish(conn)
    assert head(conn, 1) is None and head(conn, 0)['members_count'] == 1
    with conn:
        conn.execute('DELETE FROM road_trip_recipes WHERE id=0')
    finish(conn)
    assert not conn.execute('SELECT 1 FROM prepared_recipe_heads').fetchone()
    assert preparation.read_original(initial) == original
    assert Path(initial['filename']).exists()


@pytest.mark.parametrize('installed', [88], indirect=True)
def test_file_publication_is_outside_transaction_and_stale_capture_is_not_admitted(installed, monkeypatch):
    store, _ = installed
    seed(store)
    preparation.JOB.install(store.conn)
    publish = preparation._publish_encoded
    second = Store(store._db_path)
    calls = []
    try:
        def edit(root, encoded, digest):
            assert not store.conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE road_trip_recipes SET artists='[]',genres='[]',blacklist_genres='[]' WHERE id=1")
                calls.append(True)
            return publish(root, encoded, digest)
        monkeypatch.setattr(preparation, '_publish_encoded', edit)
        result = preparation.JOB.step(store.conn, batch_size=1)
        assert result.prepared_revision is None and result.input_revision == 1
        assert not store.conn.execute(f'SELECT 1 FROM {preparation.HEAD}').fetchone()
        finish(store.conn)
        assert head(store.conn)['members_count'] == 0
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [88], indirect=True)
def test_interruption_failure_restart_and_explicit_retry_keep_member_checkpoint(installed, monkeypatch):
    store, _ = installed
    original = seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    checkpoint = preparation.JOB.step(conn, batch_size=2)
    copy = preparation._copy
    def interrupted(*args):
        copy(*args)
        raise KeyboardInterrupt('controlled recipe interruption')
    monkeypatch.setattr(preparation, '_copy', interrupted)
    with pytest.raises(KeyboardInterrupt):
        preparation.JOB.step(conn, batch_size=2)
    assert preparation.JOB.progress(conn) == checkpoint
    def failed(*args):
        copy(*args)
        raise OSError('controlled recipe failure')
    monkeypatch.setattr(preparation, '_copy', failed)
    with pytest.raises(OSError, match='controlled recipe failure'):
        preparation.JOB.step(conn, batch_size=2)
    assert preparation.JOB.progress(conn).status == 'failed'
    assert conn.execute(f'SELECT count(*) FROM {preparation.ITEMS}').fetchone()[0] == 2
    regular.migrate(conn)
    monkeypatch.setattr(preparation, '_copy', copy)
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.JOB.step(reopened.conn)
        preparation.JOB.step(reopened.conn, batch_size=2, retry=True)
        finish(reopened.conn)
        assert preparation.read_original(head(reopened.conn)) == original
        assert reopened.conn.execute(f'SELECT count(*) FROM {preparation.ITEMS}').fetchone()[0] == 7
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [88], indirect=True)
@pytest.mark.parametrize('raw', ['null', '{}', '[1]', '[" "]', '["valid",null]', '[bad'])
def test_invalid_saved_selection_fails_without_modifying_source(installed, raw):
    store, _ = installed
    seed(store)
    with store.conn:
        store.conn.execute('UPDATE road_trip_recipes SET artists=? WHERE id=1', (raw,))
    preparation.JOB.install(store.conn)
    with pytest.raises((MigrationError, ValueError)):
        preparation.JOB.step(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    regular.migrate(store.conn)
    assert store.conn.execute('SELECT artists FROM road_trip_recipes WHERE id=1').fetchone()[0] == raw
    assert head(store.conn) is None


@pytest.mark.parametrize('installed', [88], indirect=True)
def test_long_recipe_and_other_source_edits_never_expand_a_member_batch(installed):
    store, _ = installed
    values = {'artists': [], 'genres': [f'Unadmitted {i}' for i in range(600)], 'blacklist_genres': []}
    original = seed(store, values)
    conn = store.conn
    with conn:
        conn.execute('INSERT INTO road_trip_recipes '
            '(id,name,own_pct,artists,genres,blacklist_genres,target_minutes,created_at,updated_at) '
            "VALUES (2,'Other trip',50,'[]','[]','[]',90,10,10)")
    preparation.JOB.install(conn)
    checkpoint = preparation.JOB.step(conn, batch_size=128)
    assert checkpoint.processed == 128 and head(conn) is None
    with conn:
        conn.execute("UPDATE road_trip_recipes SET genres='[\"Known\"]' WHERE id=2")
    prior = checkpoint.processed
    for _ in range(20):
        progress = preparation.JOB.step(conn, batch_size=128)
        assert 0 <= progress.processed - prior <= 128
        prior = progress.processed
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Long recipe did not finish')
    captured = head(conn)
    assert captured['members_count'] == 600
    assert preparation.read_original(captured) == original
    labels = [row[0] for row in conn.execute(f'SELECT raw_label FROM {preparation.ITEMS} '
        "WHERE recipe_id=1 AND generation=? AND role='include' ORDER BY position", (captured['generation'],))]
    assert labels == values['genres']
    assert head(conn, 2)['members_count'] == 1


@pytest.mark.parametrize('installed', [88], indirect=True)
def test_interruption_at_final_head_commit_rolls_back_the_complete_batch(installed):
    store, _ = installed
    seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    checkpoint = preparation.JOB.step(conn, batch_size=2)
    class InterruptCommit:
        def __getattr__(self, name):
            return getattr(conn, name)
        def commit(self):
            raise KeyboardInterrupt('controlled head publication interruption')
    with pytest.raises(KeyboardInterrupt):
        preparation.JOB.step(InterruptCommit(), batch_size=512)
    assert preparation.JOB.progress(conn) == checkpoint
    assert head(conn) is None
    assert conn.execute(f'SELECT count(*) FROM {preparation.ITEMS}').fetchone()[0] == 2
    finish(conn)
    assert head(conn)['members_count'] == 7
