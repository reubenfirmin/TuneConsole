"""Stage explicit choices and disabled defaults without retiring their owners."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from tests.store_fixture_support import numbered_store as installed, preparation_ddl_connection
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import genre_preferences as preparation
from yt_playlist.core.store import Store
from yt_playlist.util import genre_catalog


BASELINE = ['Known', 'Unknown retained', '', 'Ambiguous', 'Known']


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Preference preparation does not construct an HTTP app."""


def seed(conn):
    with conn:
        conn.executemany("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')", [
            ('known', 'Known'), ('ambiguous-one', 'Ambiguous'), ('ambiguous-two', 'Other')])
        conn.execute("INSERT INTO genre_alias VALUES ('ambiguous','Ambiguous','ambiguous-two','custom')")


def finish(job, conn):
    for _ in range(1000):
        result = job.step(conn, batch_size=2)
        if result.status == 'prepared':
            return result
    pytest.fail('Genre preference preparation did not finish')


def head(conn):
    revision = preparation.DEFAULTS.progress(conn).prepared_revision
    return conn.execute(f'SELECT * FROM {preparation.HEAD} WHERE generation=?', (revision,)).fetchone()


@pytest.mark.parametrize('installed', [79, 84], indirect=True)
def test_enabled_choices_keep_labels_custom_intent_and_case_only_renames(installed, preparation_ddl_connection):
    store, _ = installed
    conn = store.conn
    seed(conn)
    with conn:
        conn.executemany('INSERT INTO genre_whitelist VALUES (?)', [('Known',), ('Local style',), ('Ambiguous',)])
    choices = preparation.CHOICES
    choices.install(conn)
    first = choices.step(conn, batch_size=1)
    assert first.processed == 1 and first.prepared_revision is None
    regular.migrate(preparation_ddl_connection)
    finish(choices, conn)
    assert [tuple(row) for row in conn.execute('SELECT raw_label,genre_key,proposed_key,disposition '
        'FROM prepared_genre_choices ORDER BY raw_label')] == [
            ('Ambiguous', None, None, 'ambiguous_genre'), ('Known', 'known', None, 'resolved'),
            ('Local style', None, 'local style', 'requires_custom')]
    assert not conn.execute("SELECT 1 FROM genre_catalog WHERE key='local style'").fetchone()
    with conn:
        conn.execute("UPDATE genre_whitelist SET name='KNOWN' WHERE name='Known'")
        conn.execute("DELETE FROM genre_whitelist WHERE name='Local style'")
        conn.execute("INSERT INTO genre_whitelist VALUES ('Later custom')")
    finish(choices, conn)
    # The legacy primary key is NOCASE. The journal retains both spellings;
    # reconciliation must withdraw the old exact key before copying the new.
    assert not conn.execute('SELECT 1 FROM prepared_genre_choices WHERE source_key=?', ('["Known"]',)).fetchone()
    assert tuple(conn.execute('SELECT raw_label,genre_key FROM prepared_genre_choices WHERE source_key=?',
                             ('["KNOWN"]',)).fetchone()) == ('KNOWN', 'known')
    assert conn.execute('SELECT count(*) FROM prepared_genre_choices').fetchone()[0] == 3
    assert conn.execute('SELECT count(*) FROM prepared_genre_choice_originals WHERE source_key=?',
                        ('["Known"]',)).fetchone()[0] == 1
    with conn:
        conn.execute("DELETE FROM genre_alias WHERE label_key='ambiguous'")
    assert choices.progress(conn).prepared_revision is None
    finish(choices, conn)
    assert conn.execute("SELECT genre_key FROM prepared_genre_choices WHERE raw_label='Ambiguous'").fetchone()[0] == 'ambiguous-one'
    assert [row[0] for row in conn.execute('SELECT name FROM genre_whitelist ORDER BY name')] == [
        'Ambiguous', 'KNOWN', 'Later custom']
    assert not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='genre_preferences'").fetchone()


@pytest.mark.parametrize('installed', [79, 84], indirect=True)
def test_disabled_defaults_keep_exact_baseline_and_prior_generations(installed, preparation_ddl_connection):
    store, _ = installed
    conn = store.conn
    seed(conn)
    raw = json.dumps(BASELINE, ensure_ascii=False, indent=2)
    with conn:
        conn.executemany('INSERT INTO settings VALUES (?,?)', [('genre_defaults', raw), ('genres_seeded', '0')])
    preparation.DEFAULTS.install(conn)
    first = preparation.DEFAULTS.step(conn, batch_size=2)
    assert first.processed == 2 and first.status == 'copying'
    regular.migrate(preparation_ddl_connection)
    finish(preparation.DEFAULTS, conn)
    original = head(conn)
    assert preparation.read_baseline(original) == raw
    assert tuple(original[key] for key in ('baseline_origin', 'seeded_present', 'seeded_value', 'apply_defaults')) == (
        'saved', 1, '0', 1)
    rows = conn.execute(f'SELECT raw_label,genre_key,disposition,proposed_enabled FROM {preparation.ITEMS} '
                        'WHERE generation=? ORDER BY position', (original['generation'],)).fetchall()
    assert list(map(tuple, rows)) == [
        ('Known', 'known', 'resolved', 0), ('Unknown retained', None, 'unknown_genre', 0),
        ('', None, 'blank', 0), ('Ambiguous', None, 'ambiguous_genre', 0), ('Known', 'known', 'resolved', 0)]
    with conn:
        conn.execute("UPDATE settings SET value='[\"Known\"]' WHERE key='genre_defaults'")
        conn.execute("UPDATE settings SET value='' WHERE key='genres_seeded'")
    finish(preparation.DEFAULTS, conn)
    latest = head(conn)
    assert latest['generation'] > original['generation']
    assert latest['seeded_value'] == '' and latest['apply_defaults'] == 0
    assert preparation.read_baseline(original) == raw
    assert Path(original['filename']).exists()
    assert conn.execute(f'SELECT count(*) FROM {preparation.ITEMS} WHERE generation=?',
                        (original['generation'],)).fetchone()[0] == len(BASELINE)
    with conn:
        conn.execute("INSERT INTO genre_whitelist VALUES ('Known')")
    assert preparation.DEFAULTS.progress(conn).prepared_revision is None
    finish(preparation.DEFAULTS, conn)
    assert head(conn)['apply_defaults'] == 1
    assert conn.execute("SELECT value FROM settings WHERE key='genre_defaults'").fetchone()[0] == '["Known"]'
    assert not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='genre_preferences'").fetchone()


@pytest.mark.parametrize('installed', [84], indirect=True)
@pytest.mark.parametrize('marker', ['missing', None, ''])
def test_unintroduced_baseline_and_nullable_seed_marker_remain_distinct(installed, marker):
    store, _ = installed
    conn = store.conn
    if marker != 'missing':
        with conn:
            conn.execute("INSERT INTO settings VALUES ('genres_seeded',?)", (marker,))
    preparation.DEFAULTS.install(conn)
    finish(preparation.DEFAULTS, conn)
    saved = head(conn)
    assert saved['baseline_origin'] == 'legacy_defaults'
    assert json.loads(preparation.read_baseline(saved)) == genre_catalog.legacy_names()
    assert saved['seeded_present'] == int(marker != 'missing')
    assert saved['seeded_value'] == (None if marker == 'missing' else marker)
    assert saved['apply_defaults'] == 0
    assert not conn.execute(f'SELECT 1 FROM {preparation.ITEMS} WHERE proposed_enabled IS NOT NULL').fetchone()
    assert not conn.execute("SELECT 1 FROM settings WHERE key='genre_defaults'").fetchone()


@pytest.mark.parametrize('installed', [84], indirect=True)
def test_file_work_releases_transaction_and_concurrent_source_edit_is_fenced(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    with conn:
        conn.execute("INSERT INTO settings VALUES ('genre_defaults',?)", (json.dumps(BASELINE),))
    preparation.DEFAULTS.install(conn)
    publish = preparation._publish_encoded
    second = Store(store._db_path)
    calls = []
    try:
        def edit(root, encoded, digest):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE settings SET value='[]' WHERE key='genre_defaults'")
                calls.append(True)
            return publish(root, encoded, digest)
        monkeypatch.setattr(preparation, '_publish_encoded', edit)
        stale = preparation.DEFAULTS.step(conn, batch_size=1)
        assert stale.prepared_revision is None and stale.input_revision == 1
        assert not conn.execute(f'SELECT 1 FROM {preparation.HEAD}').fetchone()
        finish(preparation.DEFAULTS, conn)
        assert head(conn)['labels_count'] == 0
        assert preparation.read_baseline(head(conn)) == '[]'
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [84], indirect=True)
def test_failure_interrupt_and_restart_keep_committed_batches(installed, preparation_ddl_connection, monkeypatch):
    store, _ = installed
    conn = store.conn
    with conn:
        conn.execute("INSERT INTO settings VALUES ('genre_defaults',?)", (json.dumps(BASELINE),))
    preparation.DEFAULTS.install(conn)
    checkpoint = preparation.DEFAULTS.step(conn, batch_size=1)
    publish = preparation._publish_encoded
    def interrupted(*args):
        assert not conn.in_transaction
        raise KeyboardInterrupt('controlled file interruption')
    monkeypatch.setattr(preparation, '_publish_encoded', interrupted)
    with pytest.raises(KeyboardInterrupt):
        preparation.DEFAULTS.step(conn, batch_size=1)
    assert preparation.DEFAULTS.progress(conn) == checkpoint
    def failed(*args):
        assert not conn.in_transaction
        raise OSError('controlled disk failure')
    monkeypatch.setattr(preparation, '_publish_encoded', failed)
    with pytest.raises(OSError, match='controlled disk failure'):
        preparation.DEFAULTS.step(conn, batch_size=1)
    assert preparation.DEFAULTS.progress(conn).status == 'failed'
    assert conn.execute(f'SELECT count(*) FROM {preparation.ITEMS}').fetchone()[0] == 1
    regular.migrate(preparation_ddl_connection)
    monkeypatch.setattr(preparation, '_publish_encoded', publish)
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.DEFAULTS.step(reopened.conn)
        preparation.DEFAULTS.step(reopened.conn, batch_size=1, retry=True)
        finish(preparation.DEFAULTS, reopened.conn)
        assert json.loads(preparation.read_baseline(head(reopened.conn))) == BASELINE
        assert reopened.conn.execute(f'SELECT count(*) FROM {preparation.ITEMS}').fetchone()[0] == len(BASELINE)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [84], indirect=True)
@pytest.mark.parametrize('raw', [None, '', 'null', '{}', '[1]', '["valid",null]'])
def test_invalid_defaults_fail_durably_without_replacing_original(installed, raw):
    store, _ = installed
    with store.conn:
        store.conn.execute("INSERT INTO settings VALUES ('genre_defaults',?)", (raw,))
    preparation.DEFAULTS.install(store.conn)
    with pytest.raises((MigrationError, ValueError)):
        preparation.DEFAULTS.step(store.conn)
    assert preparation.DEFAULTS.progress(store.conn).status == 'failed'
    assert store.conn.execute("SELECT value FROM settings WHERE key='genre_defaults'").fetchone()[0] == raw
    assert not store.conn.execute(f'SELECT 1 FROM {preparation.HEAD}').fetchone()


@pytest.mark.parametrize('installed', [84], indirect=True)
def test_choice_failure_requires_explicit_retry_and_keeps_prior_batch(installed, preparation_ddl_connection):
    store, _ = installed
    with store.conn:
        store.conn.executemany('INSERT INTO genre_whitelist VALUES (?)', [('First',), ('Second',)])
    choices = preparation.CHOICES
    choices.install(store.conn)
    checkpoint = choices.step(store.conn, batch_size=1)
    def fail(*args):
        choices.copy(*args)
        raise ValueError('controlled choice failure')
    with pytest.raises(ValueError, match='controlled choice failure'):
        replace(choices, copy=fail).step(store.conn, batch_size=1)
    assert choices.progress(store.conn).status == 'failed'
    assert store.conn.execute('SELECT count(*) FROM prepared_genre_choices').fetchone()[0] == 1
    regular.migrate(preparation_ddl_connection)
    with pytest.raises(MigrationError, match='retry explicitly'):
        choices.step(store.conn)
    choices.step(store.conn, retry=True)
    finish(choices, store.conn)
    assert choices.progress(store.conn).processed == checkpoint.processed + 1
