"""Retained label owners keep serving while bounded genre staging reconciles."""
from dataclasses import replace
import json

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import albums, genre_identity
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation.genre_classifications import JOB


def finish(conn):
    for _ in range(100):
        progress = JOB.step(conn, batch_size=2)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Genre preparation did not finish bounded work')


@pytest.mark.parametrize('installed', [79, 85], indirect=True)
def test_labels_and_continuing_edits_remain_authoritative(installed):
    store, _ = installed
    conn = store.conn
    with conn:
        genre_identity.seed_catalog(conn)
        conn.executemany('INSERT INTO recordings(id,identity_key,genre) VALUES (?,?,?)',
                         [(i, f'genre-test:{i}', label) for i, label in enumerate(
                             ['Drum And Bass', '', None, 'Unregistered style', 'Ambiguous'], 1)])
        conn.executemany("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')",
                         [('ambiguous-one', 'Ambiguous'), ('ambiguous-two', 'Second')])
        conn.execute("INSERT INTO genre_alias VALUES ('ambiguous','Ambiguous','ambiguous-two','custom')")
        album = albums.observe(conn, 'release', title='Release', artist='Artist', genre='Synth Pop')
    store.discovery.upsert_discovered_artist('New artist', 1, [], [], None, 10, genre='Jazz')
    source = [tuple(r) for r in conn.execute('SELECT id,identity_key,genre FROM recordings ORDER BY id')]
    JOB.install(conn)
    assert not conn.execute('SELECT 1 FROM prepared_genre_classifications').fetchone()
    first = JOB.step(conn, batch_size=2)
    assert first.processed == 2 and first.prepared_revision is None
    regular.migrate(conn)
    finish(conn)
    assert [tuple(r) for r in conn.execute('SELECT id,identity_key,genre FROM recordings ORDER BY id')] == source
    rows = conn.execute("SELECT raw_label,genre_key,disposition FROM prepared_genre_classifications "
                        "WHERE owner='recordings' ORDER BY source_key").fetchall()
    assert [tuple(r) for r in rows] == [
        ('Drum And Bass', 'drum bass', 'resolved'), ('', None, 'blank'), (None, None, 'null'),
        ('Unregistered style', None, 'unknown_genre'), ('Ambiguous', None, 'ambiguous_genre')]
    assert conn.execute("SELECT genre_key FROM prepared_genre_classifications WHERE owner='albums' "
                        'AND source_key=?', (json.dumps([album], separators=(',', ':')),)).fetchone()[0] == 'synth pop'
    with conn:
        conn.execute("UPDATE recordings SET genre='Jazz' WHERE id=1")
        conn.execute('DELETE FROM recordings WHERE id=2')
        conn.execute("INSERT INTO recordings(id,identity_key,genre) VALUES (0,'late-low-key','Rock')")
    finish(conn)
    assert conn.execute("SELECT genre_key FROM prepared_genre_classifications WHERE owner='recordings' AND source_key='[1]'").fetchone()[0] == 'jazz'
    assert not conn.execute("SELECT 1 FROM prepared_genre_classifications WHERE owner='recordings' AND source_key='[2]'").fetchone()
    assert conn.execute("SELECT genre_key FROM prepared_genre_classifications WHERE owner='recordings' AND source_key='[0]'").fetchone()[0] == 'rock'
    assert conn.execute("SELECT COUNT(*) FROM prepared_genre_originals WHERE owner='recordings' AND source_key='[1]'").fetchone()[0] == 2
    with conn:
        conn.execute("DELETE FROM genre_alias WHERE label_key='ambiguous'")
    assert JOB.progress(conn).status == 'copying'
    finish(conn)
    assert conn.execute("SELECT genre_key FROM prepared_genre_classifications WHERE owner='recordings' AND source_key='[5]'").fetchone()[0] == 'ambiguous-one'
    assert conn.execute('SELECT genre FROM recordings WHERE id=5').fetchone()[0] == 'Ambiguous'


@pytest.mark.parametrize('installed', [85], indirect=True)
def test_interrupt_and_failed_batch_keep_committed_prefix(installed):
    store, _ = installed
    conn = store.conn
    with conn:
        conn.executemany('INSERT INTO recordings(id,identity_key,genre) VALUES (?,?,?)',
                         [(i, f'interrupt:{i}', 'Unknown') for i in range(1, 6)])
    JOB.install(conn)
    checkpoint = JOB.step(conn, batch_size=2)
    def interrupt(*args):
        JOB.copy(*args)
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        replace(JOB, copy=interrupt).step(conn, batch_size=2)
    assert JOB.progress(conn) == checkpoint
    assert conn.execute('SELECT COUNT(*) FROM prepared_genre_classifications').fetchone()[0] == 2
    def fail(*args):
        JOB.copy(*args)
        raise ValueError('controlled genre failure')
    with pytest.raises(ValueError, match='controlled genre failure'):
        replace(JOB, copy=fail).step(conn, batch_size=2)
    assert JOB.progress(conn).status == 'failed'
    assert conn.execute('SELECT COUNT(*) FROM prepared_genre_classifications').fetchone()[0] == 2
    regular.migrate(conn)
    with pytest.raises(MigrationError, match='retry explicitly'):
        JOB.step(conn)
    JOB.step(conn, retry=True, batch_size=2)
    assert finish(conn).processed == 5
