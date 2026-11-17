"""Exact SQL annotation completion, independent of file publication."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.repos import annotation_file_writes as writes


@pytest.mark.parametrize('changed', ['identity', 'run', 'taxonomy', 'revision', 'digest', 'size', 'filename'])
def test_completion_rejects_another_reservation_or_body(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        # The tiny owner retains the real destination filename admission check.
        conn.execute('CREATE TABLE audio_annotations(id INTEGER PRIMARY KEY,model_output_filename TEXT '
                     + writes.NATIVE_CHECK + ')')
        conn.execute('INSERT INTO audio_annotations VALUES (7,\'reserved\')')
        with conn:
            identity = writes.reserve(conn, 'run', 'v1', 2, ('a' * 64, 12))
        values = dict(identity=identity, run='run', taxonomy='v1', revision=2, digest='a' * 64,
                      size=12, filename=f'/root/by-id/{identity % 256:02x}/{identity}.json')
        values[changed] = dict(identity=identity + 1, run='other', taxonomy='v2', revision=3,
            digest='b' * 64, size=13, filename='/root/by-id/02/2.json')[changed]
        with pytest.raises(ValueError, match='reservation changed|reserved identity'), conn:
            writes.complete(conn, values['identity'], values['run'], values['taxonomy'], values['revision'],
                            (values['filename'], values['digest'], values['size']), 7)
        assert conn.execute('SELECT file_status,annotation_id FROM annotation_file_writes').fetchone() == ('pending', None)
        with conn:
            writes.complete(conn, identity, 'run', 'v1', 2,
                            (f'/root/by-id/{identity % 256:02x}/{identity}.json', 'a' * 64, 12), 7)
        assert conn.execute('SELECT file_status,annotation_id FROM annotation_file_writes').fetchone() == ('ready', 7)


def test_bounded_admission_does_not_rewrite_the_installed_filename_constraint():
    with closing(sqlite3.connect(':memory:')) as conn:
        sql = ('CREATE TABLE audio_annotations (id INTEGER PRIMARY KEY,model_output_filename TEXT,'
               'model_output_digest TEXT,' + writes.LEGACY_CHECK + ')')
        conn.execute(sql)
        writes.install(conn)
        writes.validate(conn)
        assert not writes.native(conn)
        with pytest.raises(ValueError, match='offline database cutover'):
            writes.reserve(conn, 'run', 'v1', 1, ('a' * 64, 12))
        assert conn.execute("SELECT sql FROM sqlite_schema WHERE name='audio_annotations'").fetchone()[0] == sql
        assert not conn.execute('SELECT 1 FROM annotation_file_writes').fetchone()
