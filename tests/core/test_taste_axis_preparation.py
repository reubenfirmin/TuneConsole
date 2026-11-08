"""Taste preparation preserves conflicts, event IDs and source write authority."""
from dataclasses import replace
import json

import pytest

from tests.core.test_predecessor_writes import installed
from tests.independent_schema_support import independent_regular_ddl
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import taste_axes as preparation


def seed(store):
    conn = store.conn
    with conn:
        conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('known','Known','custom')")
        conn.execute("INSERT INTO genre_alias VALUES ('known alias','Known alias','known','custom')")
        conn.executemany('INSERT INTO rec_weights VALUES (?,?,?)',
                         [('genre:known', 1.2, 10), ('genre:Known alias', 1.4, 11)])
        conn.executemany('INSERT INTO home_hidden_facet VALUES (?)', [('genre:known',), ('genre:Known alias',)])
        conn.executemany('INSERT INTO rec_theme VALUES (?,?,?)',
                         [('genre:known', 2, 10), ('genre:Known alias', -0.5, 11)])
        conn.executemany('INSERT INTO rec_play_grad VALUES (?,?)',
                         [('genre:known', '2026-10-28'), ('genre:Known alias', '2026-10-29')])
    store.rec_model.set_lean('genre:known', 1.3, 12)
    store.rec_signals.log_graduation('genre:known', 'play', 2, 1.1, 1.1, 10)
    store.rec_signals.log_graduation('genre:Known alias', 'rating', -0.5, 0.9, 0.99, 11)


def source(conn):
    return {owner: [dict(row) for row in conn.execute('SELECT * FROM '+owner+' ORDER BY '+','.join(keys))]
            for owner, keys in preparation.KEYS.items()}


def finish(conn):
    for _ in range(100):
        result = preparation.JOB.step(conn, batch_size=2)
        if result.status == 'prepared':
            return result
    pytest.fail('Taste preparation did not finish')


@pytest.mark.parametrize('installed', [79, 87], indirect=True)
def test_exact_sources_and_configured_conflicts_survive_bounded_copy(installed):
    store, _ = installed
    seed(store)
    conn = store.conn
    original = source(conn)
    preparation.JOB.install(conn)
    first = preparation.JOB.step(conn, batch_size=2)
    assert first.processed == 2 and first.prepared_revision is None
    finish(conn)
    assert source(conn) == original
    assert conn.execute('SELECT count(*) FROM prepared_taste_rows').fetchone()[0] == 11
    assert [tuple(row) for row in conn.execute('SELECT owner,first_source_key,second_source_key '
        'FROM prepared_taste_conflicts')] == [('rec_weights', '["genre:Known alias"]', '["genre:known"]')]
    archived = {}
    for row in conn.execute('SELECT owner,source_snapshot FROM prepared_taste_originals ORDER BY owner,source_key'):
        archived.setdefault(row['owner'], []).append(json.loads(row['source_snapshot']))
    assert archived == original
    # Additive contributions and repeated event IDs remain individual. Neither
    # an ordering choice nor the configured-value conflict loses their history.
    assert list(map(tuple, conn.execute("SELECT score,updated_at FROM prepared_taste_rows "
        "WHERE owner='rec_theme' ORDER BY source_key"))) == [(-0.5, 11), (2, 10)]
    assert list(map(tuple, conn.execute("SELECT source_key,source,score FROM prepared_taste_rows "
        "WHERE owner='rec_grad_log' ORDER BY source_key"))) == [('[1]', 'play', 2), ('[2]', 'rating', -0.5)]
    assert not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axes'").fetchone()
    with conn:
        conn.execute("DELETE FROM rec_weights WHERE axis='genre:Known alias'")
    store.rec_model.set_lean('genre:known', 1.8, 20)
    store.rec_signals.log_graduation('genre:known', 'play', 3, 1.2, 1.32, 21)
    finish(conn)
    assert not conn.execute('SELECT 1 FROM prepared_taste_conflicts').fetchone()
    assert conn.execute("SELECT count(*) FROM prepared_taste_originals WHERE owner='rec_weights'").fetchone()[0] == 2
    assert conn.execute("SELECT value FROM prepared_taste_rows WHERE owner='rec_lean'").fetchone()[0] == 1.8
    assert conn.execute("SELECT count(*) FROM prepared_taste_rows WHERE owner='rec_grad_log'").fetchone()[0] == 3
    assert store.rec_model.get_lean('genre:known') == 1.8


@pytest.mark.parametrize('installed', [79, 87], indirect=True)
def test_family_priority_unknowns_and_reference_reconciliation(installed):
    store, _ = installed
    conn = store.conn
    with conn:
        conn.execute("INSERT INTO genre_family VALUES ('shared','Shared family')")
        conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('shared','Shared genre','custom')")
        conn.executemany('INSERT INTO rec_weights VALUES (?,1,10)',
                         [('genre:shared',), ('family:shared',), ('genre:Unknown retained',), ('artist:Exact',)])
    preparation.JOB.install(conn)
    finish(conn)
    fields = {row['raw_axis']: tuple(row[name] for name in ('kind', 'genre_key', 'family_key', 'value', 'resolution_reason'))
              for row in conn.execute('SELECT r.raw_axis,a.kind,a.genre_key,a.family_key,a.value,a.resolution_reason '
                                      'FROM prepared_taste_rows r JOIN prepared_taste_axis_keys a USING(axis_key)')}
    assert fields == {
        'genre:shared': ('family', None, 'shared', None, None),
        'family:shared': ('family', None, 'shared', None, None),
        'genre:Unknown retained': ('unresolved_genre', None, None, 'Unknown retained', 'unknown_genre'),
        'artist:Exact': ('other', None, None, 'artist:Exact', None)}
    assert conn.execute('SELECT count(*) FROM prepared_taste_conflicts').fetchone()[0] == 1
    with conn:
        conn.execute("INSERT INTO genre_alias VALUES ('unknown retained','Unknown retained','shared','custom')")
    assert preparation.JOB.progress(conn).prepared_revision is None
    finish(conn)
    assert tuple(conn.execute('SELECT a.kind,a.genre_key FROM prepared_taste_rows r '
        'JOIN prepared_taste_axis_keys a USING(axis_key) WHERE r.raw_axis=?', ('genre:Unknown retained',)).fetchone()) == (
            'genre', 'shared')
    assert tuple(conn.execute('SELECT a.kind,a.value FROM prepared_taste_originals o '
        'JOIN prepared_taste_axis_keys a USING(axis_key) WHERE json_extract(o.source_snapshot,\'$.axis\')=? '
        "AND a.kind='unresolved_genre'", ('genre:Unknown retained',)).fetchone()) == (
            'unresolved_genre', 'Unknown retained')
    assert conn.execute("SELECT axis FROM rec_weights WHERE axis='genre:Unknown retained'").fetchone()[0] == 'genre:Unknown retained'


@pytest.mark.parametrize('installed', [87], indirect=True)
def test_interruption_and_invalid_batch_preserve_prefix_until_explicit_retry(installed, independent_regular_ddl):
    store, _ = installed
    seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    preparation.JOB.step(conn, batch_size=2)
    checkpoint = preparation.JOB.step(conn, batch_size=2)
    def interrupted(*args):
        preparation.JOB.copy(*args)
        raise KeyboardInterrupt('controlled taste interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(preparation.JOB, copy=interrupted).step(conn, batch_size=2)
    assert preparation.JOB.progress(conn) == checkpoint
    with conn:
        conn.execute("UPDATE rec_lean SET axis='' WHERE axis='genre:known'")
    with pytest.raises(ValueError, match='taste axis requires'):
        finish(conn)
    assert preparation.JOB.progress(conn).status == 'failed'
    assert conn.execute('SELECT count(*) FROM prepared_taste_rows').fetchone()[0] == 2
    regular.migrate(conn)
    with conn:
        conn.execute("UPDATE rec_lean SET axis='genre:known' WHERE axis=''")
    with pytest.raises(MigrationError, match='retry explicitly'):
        preparation.JOB.step(conn)
    preparation.JOB.step(conn, retry=True)
    finish(conn)
    assert conn.execute('SELECT count(*) FROM prepared_taste_rows').fetchone()[0] == 11


@pytest.mark.parametrize('installed', [87], indirect=True)
def test_source_key_change_and_late_event_behind_checkpoint_withdraw_old_rows(installed):
    store, _ = installed
    seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    finish(conn)
    with conn:
        conn.execute("UPDATE rec_grad_log SET id=0,axis='artist:Changed' WHERE id=1")
        conn.execute("UPDATE rec_lean SET axis='artist:New slider' WHERE axis='genre:known'")
    finish(conn)
    assert not conn.execute("SELECT 1 FROM prepared_taste_rows WHERE owner='rec_grad_log' AND source_key='[1]'").fetchone()
    assert conn.execute("SELECT raw_axis FROM prepared_taste_rows WHERE owner='rec_grad_log' AND source_key='[0]'").fetchone()[0] == 'artist:Changed'
    assert list(map(tuple, conn.execute("SELECT raw_axis,value FROM prepared_taste_rows WHERE owner='rec_lean'"))) == [
        ('artist:New slider', 1.3)]
    assert conn.execute("SELECT count(*) FROM prepared_taste_originals WHERE owner='rec_grad_log'").fetchone()[0] == 3
