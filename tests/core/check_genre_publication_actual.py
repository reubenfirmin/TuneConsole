"""Measure a genuine v85 fixture populated with actual selected labels and IDs.

The source is already v93: its pre-v86 installation cannot be reconstructed from
that fact. This deliberately derived fixture checks the actual label population,
not an exact historical library, its metadata chronology or its HTTP behavior.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from actual_publication_timing import WriterTimings
from yt_playlist.core import genre_identity
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.preparation import genre_classifications as preparation, genre_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.util.genre_catalog import normalize


def fingerprint(conn, owner, key):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+key):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, allow_nan=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(source, fixture, destination):
    root = Path.cwd()/'.pytest_cache'
    assert all(path.is_relative_to(root) for path in (source, fixture, destination))
    assert source != fixture and not fixture.exists()
    fixture.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'failed', 'source': str(source), 'fixture': str(fixture),
        'python': sys.version, 'sqlite': sqlite3.sqlite_version,
        'dataset': 'Genuine frozen v85 DDL populated with actual current selected genre labels and exact owner IDs from the own isolated v93 snapshot. Non-genre metadata is minimal fixture data. Original historical pre-v86 labels cannot be inferred from current selected labels.',
        'controls': 'Read-only source; no provider, native application, HTTP or background workers. Fixture genre catalog adds source aliases and identities to the seeded catalog. One same-value compatible label write forces withdrawal and reconciliation.',
        'timing': 'Maximum writer reservation from BEGIN IMMEDIATE through commit/rollback completion.'}
    actual = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)
    actual.row_factory = sqlite3.Row
    protected = {'schema_migrations': 'version', 'recordings': 'id', 'albums': 'id',
        'discovered_artists': 'artist', 'genre_catalog': 'key', 'genre_alias': 'label_key'}
    before = {owner: fingerprint(actual, owner, key) for owner, key in protected.items()}
    actual.execute('BEGIN')
    subjects = {owner: [tuple(row) for row in actual.execute('SELECT s.'+key+
        ',coalesce(g.name,s.unresolved_genre) FROM main.'+owner+' s LEFT JOIN main.genre_catalog g ON g.key=s.genre_key ORDER BY s.'+key)]
        for owner, key in preparation.genre_classifications.OWNERS.items()}
    artist_rows = list(map(tuple, actual.execute('SELECT id,name,provisional FROM main.artists ORDER BY id')))
    catalog = list(map(tuple, actual.execute('SELECT key,name,family_key,source FROM main.genre_catalog ORDER BY key')))
    families = list(map(tuple, actual.execute('SELECT key,name FROM main.genre_family ORDER BY key')))
    aliases = list(map(tuple, actual.execute('SELECT * FROM main.genre_alias ORDER BY label_key')))
    actual.rollback()
    store, timer = Store(fixture), WriterTimings()
    conn = store.conn
    try:
        runner.migrate(conn, registry=registry.MIGRATIONS[:85])
        store.init_schema(defer_metadata=True)
        with conn:
            genre_identity.seed_catalog(conn)
            conn.executemany('INSERT INTO genre_family(key,name) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name', families)
            conn.executemany('INSERT INTO genre_catalog(key,name,family_key,source) VALUES (?,?,?,?) '
                'ON CONFLICT(key) DO UPDATE SET name=excluded.name,family_key=excluded.family_key,source=excluded.source', catalog)
            conn.executemany('INSERT INTO genre_alias VALUES (?,?,?,?) ON CONFLICT(label_key) DO UPDATE SET '
                'label=excluded.label,genre_key=excluded.genre_key,source=excluded.source', aliases)
            conn.executemany('INSERT INTO artists(id,name,provisional) VALUES (?,?,?)', artist_rows)
            conn.executemany('INSERT INTO recordings(id,identity_key,genre) VALUES (?,?,?)',
                [(key, 'actual-genre-fixture:'+str(key), label) for key, label in subjects['recordings']])
            conn.executemany("INSERT INTO albums(id,title,artist,genre,metadata_rank) VALUES (?,'Fixture album','Fixture artist',?,0)", subjects['albums'])
            conn.executemany("INSERT INTO discovered_artists(artist,artist_id,genre) VALUES (?,?,?)",
                [('@artist:'+str(key), key, label) for key, label in subjects['discovered_artists']])
        history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        fixture_before = {owner: fingerprint(conn, owner, key) for owner, key in preparation.genre_classifications.OWNERS.items()}
        expected = {}
        for owner, rows in subjects.items():
            for key, label in rows:
                keys = {row[0] for row in conn.execute('SELECT key FROM genre_catalog WHERE key=? OR name=? COLLATE NOCASE '
                    'UNION SELECT genre_key FROM genre_alias WHERE label_key=? OR label=? COLLATE NOCASE',
                    (label, label, normalize(label or ''), label))} if label else set()
                genre = next(iter(keys)) if len(keys) == 1 else None
                disposition = 'null' if label is None else 'blank' if label == '' else 'resolved' if genre else 'ambiguous_genre' if keys else 'unknown_genre'
                display = conn.execute('SELECT name FROM genre_catalog WHERE key=?', (genre,)).fetchone()[0] if genre else label
                expected[owner, json.dumps([key], separators=(',', ':'))] = (label, genre, disposition, display)
        report['input_counts'] = {owner: len(rows) for owner, rows in subjects.items()}
        report['dispositions'] = dict(Counter(value[2] for value in expected.values()))
        timer.watch(conn)
        def ready():
            preparation.JOB.install(conn)
            batches = []
            for _ in range(10000):
                progress, timing = timer.timed(lambda: preparation.JOB.step(conn, batch_size=16))
                batches.append(timing)
                if progress.status == 'prepared':
                    break
            else:
                raise AssertionError('Actual-label preparation did not finish')
            maps = []
            for _ in range(10000):
                stage, timing = timer.timed(lambda: publication.step(conn, batch_size=16))
                maps.append(timing)
                if stage.complete:
                    break
            else:
                raise AssertionError('Actual-label publication did not stage')
            _, selection = timer.timed(lambda: publication.publish(conn, identity=stage.id))
            return stage.id, {'preparation': batches, 'staging': maps, 'selection': selection}
        stage, report['initial'] = ready()
        actual_rows = {(row['owner'], row['source_key']): (row['raw_label'], row['genre_key'], row['disposition'])
            for row in conn.execute('SELECT * FROM '+publication.table('rows')+' WHERE stage=?', (stage,))}
        assert actual_rows == {key: value[:3] for key, value in expected.items()}
        with store.read_view() as reader:
            reader.conn.set_authorizer(lambda operation, owner, column, *_: sqlite3.SQLITE_DENY
                if operation == sqlite3.SQLITE_READ and owner in subjects and column == 'genre' else sqlite3.SQLITE_OK)
            for owner in subjects:
                column = preparation.genre_classifications.OWNERS[owner]
                expression = preparation.genre_classifications.read_expression(reader.conn, 's', owner)
                values = {json.dumps([row[0]], separators=(',', ':')): row[1] for row in reader.conn.execute(
                    'SELECT s.'+column+','+expression+' FROM main.'+owner+' s')}
                assert values == {key: value[3] for (kind, key), value in expected.items() if kind == owner}
        _, report['regular_selected'] = timer.timed(lambda: regular.migrate(conn))
        def same_value():
            with conn:
                conn.execute('UPDATE main.recordings SET genre=genre WHERE id=(SELECT min(id) FROM main.recordings)')
        _, report['continuing_writer'] = timer.timed(same_value)
        assert publication.selected(conn) is None
        successor, report['reconciled'] = ready()
        assert successor != stage and publication.selected(conn) == successor
        assert fixture_before == {owner: fingerprint(conn, owner, key) for owner, key in preparation.genre_classifications.OWNERS.items()}
        assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        report.update(status='passed', fixture_sources_and_history_exact=True, source_read_mask_passed=True,
            independently_checked_selection_rows=len(expected), selected=successor)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timer.close()
        report['actual_sources_exact'] = before == {owner: fingerprint(actual, owner, key) for owner, key in protected.items()}
        conn.close()
        actual.close()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2)+'\n')
    assert report['actual_sources_exact']


if __name__ == '__main__':
    check(*(Path(argument).resolve() for argument in sys.argv[1:]))
