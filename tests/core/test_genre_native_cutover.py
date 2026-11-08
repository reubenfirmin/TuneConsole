"""A retained genre publication crosses the actual 79, 85 and 86 contracts."""
import pytest

from tests.core import test_genre_publication as genres
from tests.independent_schema_support import independent_regular_ddl
from tests.prepared_fixture_support import assert_upgrade_phase
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import registry, regular, runner


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """SQL/file publication does not start the HTTP lifecycle."""


def test_genre_native_input_has_real_installed_history_and_guards(_bounded_installed_schema):
    assert_upgrade_phase(_bounded_installed_schema(79), 79)


@pytest.fixture(scope='module')
def published_genres(_bounded_installed_schema, tmp_path_factory):
    template = _bounded_installed_schema(79)
    path = tmp_path_factory.mktemp('genre-native-cutover')/'source.db'
    with template.open(path) as store:
        genres.seed(store)
        stage = genres.ready(store.conn)
        genres.publication.publish(store.conn, identity=stage)
        rows = list(map(tuple, store.conn.execute('SELECT * FROM '+genres.publication.table('rows'))))
        assert rows and genres.publication.selected(store.conn) == stage
        # Numbered migration owns physical SQL without connection-local serving
        # adapters. Preserve the actual files and publication in the same DB.
        Revisions.unbind(store.conn)
        adapters = list(store.conn.execute("SELECT type,name FROM sqlite_temp_schema "
            "WHERE type IN ('trigger','view') ORDER BY type"))
        for kind, name in adapters:
            quoted = '"' + name.replace('"', '""') + '"'
            store.conn.execute(f'DROP {kind.upper()} IF EXISTS temp.{quoted}')
        yield dict(conn=store.conn, rows=rows, stage=stage)


def assert_retained(state, version):
    conn = state['conn']
    assert list(map(tuple, conn.execute('SELECT * FROM '+genres.publication.table('rows')))) == state['rows']
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (migration.version, migration.name) for migration in registry.MIGRATIONS[:version]]
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_genre_publication_input_keeps_all_exact_null_blank_unknown_and_ambiguous_sources(published_genres):
    assert_retained(published_genres, 79)
    assert {row['disposition'] for row in published_genres['conn'].execute(
        'SELECT disposition FROM '+genres.publication.table('rows'))} >= {
            'resolved', 'null', 'blank', 'unknown_genre', 'ambiguous_genre'}


@pytest.fixture(scope='module')
def predecessor_85(published_genres):
    runner.migrate(published_genres['conn'], registry=registry.MIGRATIONS[:85])
    return published_genres


def test_actual_predecessor_upgrade_keeps_the_retained_genre_selection(predecessor_85):
    assert_retained(predecessor_85, 85)
    assert genres.publication.selected(predecessor_85['conn']) == predecessor_85['stage']


@pytest.fixture(scope='module')
def native_86(predecessor_85):
    runner.migrate(predecessor_85['conn'], registry=registry.MIGRATIONS[:86])
    return predecessor_85


def test_native_genre_takeover_retires_selection_without_losing_original_rows(native_86):
    assert_retained(native_86, 86)
    assert genres.publication.selected(native_86['conn']) is None


def test_regular_admission_keeps_native_genre_precedence_and_retained_selection_inputs(
        native_86, independent_regular_ddl):
    # Registry ordering has real owners in test_independent_schema_changes.
    # This publication consumer needs a genuine independent DDL change.
    regular.migrate(native_86['conn'])
    assert regular.validate(native_86['conn']) == 1
    assert_retained(native_86, 86)
    assert genres.publication.selected(native_86['conn']) is None
