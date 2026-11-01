"""Canonical ownership, basis separation and exact legacy vector retention."""
import json
import sqlite3
import struct

import pytest

from yt_playlist.core import embeddings
from yt_playlist.core.migrations import Migration, MigrationError, migrate, registry
from yt_playlist.core.migrations import v068_canonical_embeddings as migration


UPGRADE = Migration(68, 'canonical_embeddings', migration.apply)


def test_artist_svd_basis_is_independent_of_the_track_model_configuration():
    first = {'rec_artist_fit_inputs': 'artist-baskets', 'rec_fit_inputs': 'recording-baskets',
             'rec_dim': '128', 'rec_embed_method': 'cooc'}
    second = {**first, 'rec_dim': '48', 'rec_embed_method': 'svd'}
    assert embeddings.version('rec_artist_vectors', first) == embeddings.version('rec_artist_vectors', second)
    assert embeddings.version('rec_vectors', first) != embeddings.version('rec_vectors', second)


@pytest.fixture
def predecessor(tmp_path):
    conn = sqlite3.connect(tmp_path / 'embeddings.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    migrate(conn, registry=registry.MIGRATIONS[:67])
    with conn:
        conn.executemany('INSERT INTO recordings(id,identity_key) VALUES (?,?)', [(1, 'one'), (2, 'two')])
        conn.executemany('INSERT INTO artists(id,name,provisional) VALUES (?,?,?)',
                         [(1, 'Provisional', 1), (2, 'Collaborator', 0), (3, 'Other', 0)])
        conn.executemany('INSERT INTO artist_aliases VALUES (?,?,?)',
                         [(1, 'same name', 'fixture'), (2, 'same name', 'fixture')])
        conn.execute("INSERT INTO settings VALUES ('rec_content_model',?)",
                     (json.dumps({'ncat': 2, 'cont': []}),))
        for source, (kind, _space, _column) in migration.SOURCES.items():
            keys = ['two', 'one'] if kind == 'recording' else ['@artist:2', '@artist:1']
            conn.executemany(f'INSERT INTO {source} VALUES (?,?)',
                             [(key, struct.pack('ff', i + 1, .5)) for i, key in enumerate(keys)])
    yield conn
    conn.close()


def upgrade(conn):
    migrate(conn, registry=(*registry.MIGRATIONS[:67], UPGRADE))


def test_exact_owners_vectors_order_and_shared_content_basis(predecessor):
    conn = predecessor
    originals = {source: list(map(tuple, conn.execute(
        f'SELECT {column},vec FROM {source}' + ('' if space == 'content_discovery' else f' ORDER BY {column}'))))
        for source, (_kind, space, column) in migration.SOURCES.items()}
    upgrade(conn)
    migration.validate(conn)
    assert {source: embeddings.rows(conn, source) for source in originals} == originals
    assert embeddings.head(conn, 'rec_content_vectors') == embeddings.head(conn, 'rec_discovered_content_vectors')
    assert conn.execute('SELECT COUNT(*) FROM recording_embeddings').fetchone()[0] == 6
    assert conn.execute('SELECT COUNT(*) FROM artist_embeddings').fetchone()[0] == 4
    assert not conn.execute('SELECT 1 FROM unresolved_legacy_embeddings').fetchone()
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert not conn.execute('SELECT 1 FROM recording_embeddings WHERE built_at IS NOT NULL').fetchone()
    # A stable restart is validation, not another migration or vector rewrite.
    before = conn.total_changes
    upgrade(conn)
    assert conn.total_changes == before


def test_unresolved_originals_retain_bytes_without_guessing_names(predecessor):
    conn = predecessor
    blob = struct.pack('ff', 3, 4)
    with conn:
        conn.execute('INSERT INTO rec_vectors VALUES (?,?)', ('unobserved', blob))
        conn.execute('INSERT INTO rec_artist_vectors VALUES (?,?)', ('same name', blob))
        conn.execute("INSERT INTO artist_provider_ids(artist_id,provider,provider_id) "
                     "VALUES (2,'youtube','canonical-collaborator')")
        conn.execute('UPDATE artists SET provisional=1 WHERE id=3')
        conn.execute("INSERT INTO artist_redirects VALUES (3,2,'fixture',1)")
        conn.execute('INSERT INTO rec_artist_content_vectors VALUES (?,?)', ('@artist:3', blob))
    upgrade(conn)
    assert list(map(tuple, conn.execute('SELECT raw_key,vector,reason FROM unresolved_legacy_embeddings '
                                       'ORDER BY raw_key'))) == [
        ('@artist:3', blob, 'retired_owner'), ('same name', blob, 'ambiguous_owner'),
        ('unobserved', blob, 'missing_owner')]
    assert embeddings.count(conn, 'rec_vectors') == 2
    assert embeddings.count(conn, 'rec_artist_vectors') == 2
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_two_legacy_aliases_for_one_owner_are_both_retained_without_selecting_a_winner(predecessor):
    conn = predecessor
    blob = struct.pack('ff', 3, 4)
    with conn:
        conn.execute("INSERT INTO artist_aliases VALUES (1,'exact alias','fixture')")
        conn.execute('INSERT INTO rec_artist_vectors VALUES (?,?)', ('exact alias', blob))
    upgrade(conn)
    rows = [tuple(row) for row in conn.execute('SELECT raw_key,reason FROM unresolved_legacy_embeddings ORDER BY raw_key')]
    assert rows == [('@artist:1', 'ambiguous_owner'), ('exact alias', 'ambiguous_owner')]
    assert embeddings.rows(conn, 'rec_artist_vectors') == [('@artist:2', struct.pack('ff', 1, .5))]


def test_declared_encoder_must_match_legacy_vector_dimensions(predecessor):
    conn = predecessor
    with conn:
        conn.execute("UPDATE settings SET value=? WHERE key='rec_content_model'",
                     (json.dumps({'ncat': 3, 'cont': []}),))
    before = list(conn.iterdump())
    with pytest.raises(MigrationError, match='dimensions do not match the encoder'):
        upgrade(conn)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize('blob', [b'bad', b'', struct.pack('ff', float('nan'), 1), struct.pack('ff', float('inf'), 1)])
def test_malformed_vectors_fail_before_schema_replacement(predecessor, blob):
    conn = predecessor
    with conn:
        conn.execute('UPDATE rec_vectors SET vec=? WHERE identity_key=?', (blob, 'one'))
    before = list(conn.iterdump())
    with pytest.raises(MigrationError, match='float32'):
        upgrade(conn)
    assert list(conn.iterdump()) == before


def test_database_rejects_missing_owners_and_mixed_bases(predecessor):
    conn = predecessor
    upgrade(conn)
    version, dim = embeddings.head(conn, 'rec_vectors')
    blob = struct.pack('ff', 3, 4)
    for args in [(99, 'collaborative', version, dim, blob, 1),
                 (2, 'collaborative', 'incompatible', dim, blob, 1),
                 (2, 'collaborative', version, 3, blob, 1),
                 (2, 'wrong', version, dim, blob, 1)]:
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute('INSERT INTO recording_embeddings VALUES (?,?,?,?,?,?)', args)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.IntegrityError, match='owner'):
            embeddings.publish(conn, 'rec_vectors', [('unknown', blob)], replace=True)
    assert embeddings.count(conn, 'rec_vectors') == 2


def test_same_dimensions_do_not_make_unknown_encoders_compatible(predecessor):
    conn = predecessor
    with conn:
        conn.execute("DELETE FROM settings WHERE key='rec_content_model'")
    upgrade(conn)
    assert embeddings.head(conn, 'rec_content_vectors')[1] == embeddings.head(conn, 'rec_discovered_content_vectors')[1]
    assert embeddings.head(conn, 'rec_content_vectors')[0] != embeddings.head(conn, 'rec_discovered_content_vectors')[0]


@pytest.mark.parametrize('kind', ['recording', 'artist'])
def test_retirement_removes_every_space_and_rejects_readmission(predecessor, kind):
    conn = predecessor
    upgrade(conn)
    table = kind + '_embeddings'
    previous = conn.execute(f'SELECT * FROM {table} WHERE {kind}_id=1').fetchall()
    survivor = conn.execute(f'SELECT * FROM {table} WHERE {kind}_id=2').fetchall()
    assert len(previous) == (3 if kind == 'recording' else 2)
    with conn:
        if kind == 'artist':
            conn.execute("INSERT INTO artist_provider_ids(artist_id,provider,provider_id) "
                         "VALUES (2,'youtube','canonical-collaborator')")
            conn.execute("INSERT INTO artist_redirects VALUES (1,2,'fixture',1)")
        else:
            conn.execute("INSERT INTO recording_redirects VALUES (1,2,'fixture','exact correction')")
    assert not conn.execute(f'SELECT 1 FROM {table} WHERE {kind}_id=1').fetchone()
    assert conn.execute(f'SELECT * FROM {table} WHERE {kind}_id=2').fetchall() == survivor
    for original in previous:
        with pytest.raises(sqlite3.IntegrityError, match='owner is retired'), conn:
            conn.execute(f'INSERT INTO {table} VALUES (?,?,?,?,?,?)', tuple(original))
    with pytest.raises(sqlite3.IntegrityError, match='owner is retired'), conn:
        conn.execute(f'UPDATE {table} SET {kind}_id=1 WHERE {kind}_id=2')
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
