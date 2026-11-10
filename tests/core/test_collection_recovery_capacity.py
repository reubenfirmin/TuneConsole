"""Bounded collection and private cloning with 1,024 real recovery documents."""
from contextlib import closing
from pathlib import Path
import sqlite3
import time

import pytest

from tests.prepared_fixture_support import capture_template, prepared_template, roots
from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.core import collection_recovery as recovery


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """File collection consumers do not start application workers."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('recipe-capacity-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        yield


@pytest.fixture(scope='module')
def recipe_schema():
    with bounded_consumer_schema((('rock', 'Rock'), ('jazz', 'Jazz'))) as schema:
        with prepared_template(schema, lambda store: None) as template:
            yield template


def test_recipe_capacity_schema_has_real_empty_owners_and_revision_guards(recipe_schema):
    source = recipe_schema.source
    assert source.conn.execute('SELECT COUNT(*) FROM rec_recipes').fetchone()[0] == 0
    assert source.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert recipe_schema.revision_guards


@pytest.fixture(scope='module')
def recipe_documents(recipe_schema, tmp_path_factory):
    with recipe_schema.open(tmp_path_factory.mktemp('recipe-capacity-source') / 'source.db') as store:
        # These consumers receive completed lower producer input. Publish every
        # real FAO document and batch only its completed relational coordinates;
        # ordinary recipe mutations have separate controller/publication owners.
        rows = [(index + 1, f'owned-{index}', f'{index:032x}', 1, 'fixture',
                 *recovery.publish(store.conn, 'playlist-recipe', index + 1,
                                   {'model': 'fixture', 'value': index}))
                for index in range(1024)]
        with store.conn.atomic():
            store.conn.executemany("INSERT INTO playlist_scopes(id,provider_playlist_id,kind) VALUES (?,?,'shared')",
                                   [(row[1], row[1]) for row in rows])
            store.conn.executemany('INSERT INTO rec_recipes(id,playlist_scope_id,revision,created_at,model,'
                'recovery_filename,recovery_digest,recovery_bytes) VALUES (?,?,?,?,?,?,?,?)', rows)
        yield capture_template(store, 1024)


def test_recipe_capacity_source_retains_all_1024_real_files_and_sql_owners(recipe_documents):
    rows = list(recovery.references(recipe_documents.source.conn))
    assert len(rows) == 1024
    assert all(Path(row[3]).is_file() for row in rows)
    assert not recipe_documents.source.conn.execute('PRAGMA foreign_key_check').fetchall()
    assert recipe_documents.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'), recipe_documents.source.conn.atomic():
        recipe_documents.source.settings.set_setting('private owner fixture', 'forbidden')


@pytest.fixture(scope='module')
def recipe_first(recipe_documents, tmp_path_factory):
    with recipe_documents.open(tmp_path_factory.mktemp('recipe-capacity-first') / 'first.db') as store:
        yield store


def test_recipe_capacity_first_clone_keeps_complete_private_coordinates(recipe_first, recipe_documents):
    rows = list(recovery.references(recipe_first.conn))
    assert len(rows) == recipe_documents.result == 1024
    assert all(Path(row[3]).is_relative_to(recovery.directory(recipe_first.conn)) for row in rows)
    assert recipe_first.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert not recipe_first.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module')
def recipe_second(recipe_documents, tmp_path_factory):
    with recipe_documents.open(tmp_path_factory.mktemp('recipe-capacity-second') / 'second.db') as store:
        yield store


def test_recipe_capacity_second_clone_keeps_complete_private_coordinates(recipe_second, recipe_documents):
    rows = list(recovery.references(recipe_second.conn))
    assert len(rows) == recipe_documents.result == 1024
    assert all(Path(row[3]).is_relative_to(recovery.directory(recipe_second.conn)) for row in rows)
    assert not recipe_second.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('consumer', ['source', 'first', 'second'])
def test_recipe_capacity_decodes_every_retained_document(consumer, recipe_documents, recipe_first, recipe_second):
    owner = {'source': recipe_documents.source, 'first': recipe_first, 'second': recipe_second}[consumer]
    rows = list(recovery.references(owner.conn))
    assert len(rows) == 1024
    for table, identity, kind, filename, digest, size in rows:
        assert (table, kind) == ('rec_recipes', 'playlist-recipe')
        assert recovery.read(filename, digest, size, kind=kind, owner=identity) == {
            'model': 'fixture', 'value': identity - 1}


def test_recipe_capacity_clones_copy_every_file_with_distinct_inodes(recipe_documents, recipe_first, recipe_second):
    destinations = [roots(owner.conn) for owner in (recipe_first, recipe_second)]
    copied = 0
    for name, directory in recipe_documents.files.items():
        assert len({directory, *(owned[name] for owned in destinations)}) == 3
        for source in directory.rglob('*'):
            if not source.is_file():
                continue
            paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
            assert all(path.read_bytes() == source.read_bytes() for path in paths)
            assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
            copied += 1
    assert copied >= 1024


def test_recipe_capacity_clones_keep_file_corruption_and_sql_mutations_private(recipe_documents, recipe_first, recipe_second):
    references = [list(recovery.references(owner.conn)) for owner in
                  (recipe_documents.source, recipe_first, recipe_second)]
    original = Path(references[0][0][3]).read_bytes()
    assert all(Path(rows[0][3]).read_bytes() == original for rows in references)
    Path(references[1][0][3]).write_bytes(b'Private corrupted consumer recipe')
    recipe_first.settings.set_setting('private owner fixture', 'first consumer')
    recipe_first.playlist_recipes.set_recipe('owned-1', {'model': 'private replacement'}, 2)
    for owner in (recipe_documents.source, recipe_second):
        assert owner.settings.get_setting('private owner fixture') is None
        assert owner.playlist_recipes.get_recipe('owned-1') == {'model': 'fixture', 'value': 1}
    assert Path(references[0][0][3]).read_bytes() == Path(references[2][0][3]).read_bytes() == original


@pytest.fixture(scope='module')
def recipe_collection(recipe_documents, tmp_path_factory):
    with recipe_documents.open(tmp_path_factory.mktemp('recipe-capacity-collection') / 'collection.db') as store:
        with closing(sqlite3.connect(store._db_path, timeout=0)) as other, pytest.MonkeyPatch.context() as patch:
            patch.setattr(recovery.OrphanCollector, 'LIMIT', 8)
            held = {Path(row[3]) for row in recovery.references(store.conn)}
            orphaned = {Path(recovery.publish(store.conn, 'playlist-recipe', f'orphan-{index}', {'value': index})[0])
                        for index in range(45)}
            state = {'store': store, 'other': other, 'held': held, 'orphaned': orphaned, 'queries': [], 'elapsed': []}
            store.conn.set_trace_callback(state['queries'].append)
            try:
                yield state
            finally:
                store.conn.set_trace_callback(None)
                recovery.close(store)


def test_recipe_capacity_collection_input_keeps_all_owners_and_45_orphans(recipe_collection):
    assert len(recipe_collection['held']) == 1024 and len(recipe_collection['orphaned']) == 45
    assert all(path.is_file() for path in recipe_collection['held'] | recipe_collection['orphaned'])


@pytest.fixture(scope='module')
def recipe_collected(recipe_collection):
    store, other = recipe_collection['store'], recipe_collection['other']
    for tick in range(2000):
        started = time.monotonic()
        removed = store.retention.maintain_collection_recovery()
        recipe_collection['elapsed'].append(time.monotonic() - started)
        assert 0 <= removed <= 8
        assert not store.conn.in_transaction
        # One genuinely independent connection proves both reads and immediate
        # writer reservation after every turn, without repeated Store creation.
        assert other.execute('SELECT COUNT(*) FROM rec_recipes').fetchone()[0] == 1024
        other.execute('BEGIN IMMEDIATE')
        with other:
            other.execute("INSERT INTO settings VALUES ('gc-probe','usable') "
                          'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
        if tick == 3:
            recovery.close(store)  # Lose only the disposable cursor and resume.
            continue
        if not store.retention.collection_recovery_pending():
            break
    else:
        pytest.fail('Collection stopped making progress')
    return recipe_collection


def test_recipe_capacity_collection_bounds_each_turn_and_allows_independent_writes(recipe_collected):
    store = recipe_collected['store']
    assert len(recipe_collected['elapsed']) > 4
    assert not store.retention.collection_recovery_pending()
    assert store.settings.get_setting('gc-probe') == 'usable'


def test_recipe_capacity_collection_keeps_all_1024_owned_files_and_removes_all_orphans(recipe_collected):
    assert all(path.is_file() for path in recipe_collected['held'])
    assert not any(path.exists() for path in recipe_collected['orphaned'])
    store = recipe_collected['store']
    assert store.conn.execute('SELECT COUNT(*) FROM rec_recipes').fetchone()[0] == 1024
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_recipe_capacity_collection_scans_only_bounded_keyset_pages(recipe_collected):
    ownership = [sql for sql in recipe_collected['queries'] if sql.startswith('WITH page AS MATERIALIZED')]
    assert ownership and all(' IN (' in sql and ' LIMIT 2048)' in sql for sql in ownership)
