"""Repacking preserves owned ranges, transaction rollback and reader lifetime."""
from pathlib import Path

import pytest

from tests.repos.test_compact_group_publication import database
from yt_playlist.core import group_documents, policy_documents
from yt_playlist.core import group_reads as group_queries


def test_repacking_and_collection_preserve_exact_owned_content(database):
    store, _ = database
    conn = store.conn
    expected = group_queries.head(conn, members=True)
    row = conn.execute('SELECT * FROM group_versions ORDER BY id LIMIT 1').fetchone()
    original, age = group_documents.read(row['definition_filename'], row['definition_digest'],
        row['definition_bytes'], row['definition_offset'], 'definition', with_age=True)
    root = group_documents.directory(conn)
    # The current document shares a physical pack with an obsolete large body.
    unused, owned = list(group_documents.publish_many(root,
        [('definition', {'obsolete': 'x' * 200_000}), ('definition', original)], age=age))
    assert unused[0] == owned[0]
    with conn:
        conn.execute('UPDATE group_versions SET definition_filename=?,definition_offset=? WHERE id=?',
                     (owned[0], owned[3], row['id']))
    old_reference = tuple(conn.execute('SELECT definition_filename,definition_offset FROM group_versions WHERE id=?',
                                      (row['id'],)).fetchone())
    with pytest.raises(RuntimeError, match='failed bookmark'):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            group_documents.repack_batch(conn, 'group_versions', limit=1)
            raise RuntimeError('failed bookmark')
    assert tuple(conn.execute('SELECT definition_filename,definition_offset FROM group_versions WHERE id=?',
                              (row['id'],)).fetchone()) == old_reference
    assert group_queries.head(conn, members=True) == expected
    with policy_documents.retained_reader(root):
        for table, _keys, _prefix, _kind in group_documents.OWNERS:
            after = None
            while True:
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    after, count, done = group_documents.repack_batch(conn, table, after, limit=1)
                assert count <= 1
                if done:
                    break
        assert group_documents.collect_orphans(conn) == 0
        assert Path(owned[0]).exists()
    assert group_documents.collect_orphans(conn) > 0
    assert not Path(owned[0]).exists()
    assert group_queries.head(conn, members=True) == expected
    references = list(group_documents.references(conn))
    assert set(root.glob('*/*.json')) == {Path(reference[3]) for reference in references}
    for _table, _key, kind, filename, digest, size, offset in references:
        _value, copied_age = group_documents.read(filename, digest, size, offset, kind, with_age=True)
        if digest == owned[1]:
            assert (copied_age.mtime_ns, copied_age.ctime_ns) == (age.mtime_ns, age.ctime_ns)
