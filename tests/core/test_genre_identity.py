"""Alias collisions must never attribute provider evidence to an arbitrary genre."""
import pytest

from yt_playlist.core import genre_identity, metadata_values


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


@pytest.mark.parametrize('label', ['', ' ', '!!!', None, 17, 'x' * 1025])
def test_invalid_custom_addition_has_no_dml(store, label):
    before = store.conn.total_changes
    with pytest.raises(ValueError):
        genre_identity.add_custom(store.conn, label)
    assert store.conn.total_changes == before
