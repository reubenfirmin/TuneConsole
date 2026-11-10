"""Alias collisions must never attribute provider evidence to an arbitrary genre."""
import pytest

from tests.genre_catalog_support import bound_shipped_catalog
from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.core import genre_identity, metadata_values


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Genre SQL tests do not construct the web application."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture(autouse=True)
def genre_inputs(store, monkeypatch):
    bound_shipped_catalog(monkeypatch, ('synth pop', 'drum bass', 'techno'))
    with store.conn:
        genre_identity.seed_catalog(store.conn)


def test_exact_key_name_and_alias_must_agree(store):
    conn = store.conn
    conn.executemany("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')",
                     [('one', 'First'), ('two', 'Second')])
    conn.execute("INSERT INTO genre_alias VALUES ('first','First','two','custom')")
    assert genre_identity.resolve(conn, 'First') == (None, 'ambiguous_genre')
    value = metadata_values.prepare(conn, 'genre', 'First')
    assert value.text == 'First'
    assert value.genre_key is None and value.resolution_reason == 'ambiguous_genre'
    with pytest.raises(ValueError, match='Ambiguous'):
        genre_identity.add_custom(conn, 'First')


def test_only_registered_equivalences_resolve(store):
    store.genres.configure_catalog()
    conn = store.conn
    assert genre_identity.resolve(conn, 'Drum And Bass') == ('drum bass', None)
    assert genre_identity.resolve(conn, 'Synth Pop') == ('synth pop', None)
    assert genre_identity.resolve(conn, 'Fix Artist Credits') == (None, 'unknown_genre')
    assert genre_identity.resolve(conn, '') == (None, None)
    assert genre_identity.resolve(conn, None) == (None, None)


def test_custom_addition_is_explicit_and_reuses_registered_alias(store):
    store.genres.configure_catalog()
    conn = store.conn
    before = conn.execute('SELECT count(*) FROM genre_catalog').fetchone()[0]
    assert genre_identity.add_custom(conn, 'Drum And Bass') == 'drum bass'
    assert conn.execute('SELECT count(*) FROM genre_catalog').fetchone()[0] == before
    key = genre_identity.add_custom(conn, 'Local listening style')
    assert genre_identity.resolve(conn, 'local listening style') == (key, None)
    assert conn.execute('SELECT source FROM genre_catalog WHERE key=?', (key,)).fetchone()[0] == 'custom'


def test_batched_axes_preserve_alias_collisions_redirects_and_unknowns(store):
    conn = store.conn
    conn.executemany("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')",
                     [('one', 'First'), ('two', 'Second'), ('old', 'Retired')])
    conn.execute("INSERT INTO genre_alias VALUES ('first','First','two','custom')")
    conn.execute("INSERT INTO genre_redirects VALUES ('old','two')")
    labels = ['First', 'Second', 'Retired', 'old', 'one', 'Techno', 'Novel unknown style']
    expected = {label: (genre_identity.family(conn, label), genre_identity.resolve(conn, label)[0])
                for label in labels}
    queries = []
    conn.set_trace_callback(queries.append)
    try:
        assert store.genres.feature_axes(labels + labels + ['', None]) == expected
    finally:
        conn.set_trace_callback(None)
    assert len(queries) <= 2


def test_large_label_batch_does_not_rescan_all_aliases_for_each_label(store):
    conn = store.conn
    with conn:
        conn.executemany("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')",
                         [(f'local-{i}', f'Local style {i}') for i in range(2000)])
        conn.executemany("INSERT INTO genre_alias VALUES (?,?,?,'custom')",
                         [(f'local-alias-{i}', f'Local alias {i}', f'local-{i % 2000}')
                          for i in range(8000)])
        conn.execute("INSERT INTO genre_alias VALUES ('local style 3','Local style 3','local-7','custom')")
        conn.execute("INSERT INTO genre_redirects VALUES ('local-8','local-9')")
    labels = [label for i in range(40) for label in (f'Local style {i}', f'LOCAL ALIAS {i}')]
    expected = {label: (genre_identity.family(conn, label), genre_identity.resolve(conn, label)[0])
                for label in labels}
    steps = 0

    def bound_work():
        nonlocal steps
        steps += 1000
        return steps >= 1000000

    conn.set_progress_handler(bound_work, 1000)
    try:
        assert store.genres.feature_axes(labels) == expected
    finally:
        conn.set_progress_handler(None, 0)


@pytest.mark.parametrize('label', ['', ' ', '!!!', None, 17, 'x' * 1025])
def test_invalid_custom_addition_has_no_dml(store, label):
    before = store.conn.total_changes
    with pytest.raises(ValueError):
        genre_identity.add_custom(store.conn, label)
    assert store.conn.total_changes == before
