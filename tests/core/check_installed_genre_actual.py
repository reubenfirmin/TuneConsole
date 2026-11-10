"""Check bounded v92 repair with actual axis rows in a known early v88 schema."""
import json
from pathlib import Path
import sqlite3
import sys

from tests.core.actual_publication_timing import WriterTimings
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core.migrations import current, registry, regular
from yt_playlist.core.migrations import v092_installed_genre_contract as historical
from yt_playlist.core.preparation import domain_contracts, installed_genre_contract as preparation


def check(source, fixture, report_path):
    fixture.parent.mkdir(parents=True,exist_ok=True)
    actual = sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)
    actual.row_factory = sqlite3.Row
    conn = sqlite3.connect(fixture,factory=DiagnosticConnection)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    report = {'source':str(source),'fixture':str(fixture),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'scope':'Actual current axes and genre references copied into the exact supported early v88 schema; '
            'not the historical v88 catalog chronology.'}
    timer = WriterTimings()
    try:
        for sql in historical.legacy_statements(current.statements(version=88)):
            conn.execute(sql)
        with conn:
            for owner in ('genre_family','genre_catalog','genre_alias','genre_redirects','taste_axes'):
                columns = ','.join(row[1] for row in conn.execute('PRAGMA main.table_info('+owner+')'))
                conn.executemany('INSERT INTO '+owner+'('+columns+') VALUES ('+
                    ','.join('?' for _ in columns.split(','))+')',actual.execute('SELECT '+columns+' FROM '+owner))
            conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
            conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                [(item.version,item.name) for item in registry.MIGRATIONS[:88]])
        source_rows = list(map(tuple,actual.execute('SELECT * FROM taste_axes ORDER BY id')))
        history = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        original_sql = conn.execute("SELECT sql FROM sqlite_schema WHERE name='taste_axes'").fetchone()[0]
        timer.watch(conn)
        def deny(action,owner,column,*rest):
            return sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_READ,sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) and owner=='taste_axes' else sqlite3.SQLITE_OK
        conn.set_authorizer(deny)
        try:
            _,report['regular_repair'] = timer.timed(lambda:regular.migrate(conn))
        finally:
            conn.set_authorizer(None)
        selected = preparation.job(conn)
        _,report['audit_installation'] = timer.timed(lambda:selected.install(conn))
        timings = []
        for _ in range(len(source_rows)+10):
            progress,timing = timer.timed(lambda:selected.step(conn,batch_size=32))
            timings.append(timing)
            if progress.status=='prepared':
                break
        else:
            raise AssertionError('Axis audit did not converge')
        domain_contracts.verify(conn,selected)
        report['audit'] = {'steps':len(timings),'rows':len(source_rows),
            'seconds':sum(value['seconds'] for value in timings),
            'max_writer_seconds':max(value['max_writer_seconds'] for value in timings)}
        assert list(map(tuple,conn.execute('SELECT * FROM taste_axes ORDER BY id')))==source_rows
        assert conn.execute("SELECT sql FROM sqlite_schema WHERE name='taste_axes'").fetchone()[0]==original_sql
        assert list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))==history
        assert list(map(tuple,actual.execute('SELECT * FROM taste_axes ORDER BY id')))==source_rows
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        report.update(status='passed',rows_and_history_exact=True,regular_axis_population_access_denied=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timer.close()
        actual.close()
        conn.close()
        report_path.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    check(*(Path(value) for value in sys.argv[1:]))
