"""Measure direct retained copying from an existing isolated, unpruned library.

This experiment opens its source read-only and does not install its result.
Concurrent source commits reject the copy. Use a dedicated isolated library for
the separate application/cutover experiment; this read-only phase can share an
existing library snapshot without altering another experiment's fixture.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import copy_compaction, retained_copy


def check(source,output):
    source,output = source.resolve(strict=True),output.absolute()
    assert source.is_relative_to(Path('.pytest_cache').resolve())
    assert output.parent.is_relative_to(Path('.pytest_cache').resolve())
    output.parent.mkdir(parents=True,exist_ok=True)
    report = dict(source=str(source),output=str(output),python=sys.version,sqlite=sqlite3.sqlite_version,
        scope='Existing actual-library isolated snapshot, no source deletion, no historical conversion, '
              'read-only direct copy only; installation and application maintenance require separate evidence.')
    started,last = time.monotonic(),0
    def progress(phase,detail):
        nonlocal last
        if time.monotonic()-last>10:
            print(json.dumps(dict(phase=phase,detail=detail,seconds=time.monotonic()-started)),flush=True)
            last = time.monotonic()
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            report['source_sql_bytes'] = source.stat().st_size
            try:
                report['source_files'] = copy_compaction._owned_files(conn)
                report['file_custody_established'] = True
            except FileNotFoundError as error:
                # A borrowed native path can have expired after this isolated
                # SQL snapshot was taken. Measure SQL copying separately while
                # preserving that explicit installation blocker in the report.
                report.update(file_custody_established=False,file_inventory_error=str(error))
            report['source_rows'] = dict(conn.execute("SELECT name,0 FROM sqlite_schema WHERE type='table' "
                                                     "AND name NOT GLOB 'sqlite_*'"))
            for table in report['source_rows']:
                report['source_rows'][table] = conn.execute('SELECT count(*) FROM '+retained_copy.quote(table)).fetchone()[0]
        report['copy'] = retained_copy.build(source,output,progress=progress)
        report['status'] = 'sql-passed' if not report['file_custody_established'] else 'passed'
    except BaseException as error:
        report.update(status='failed',error=repr(error))
        raise
    finally:
        report['experiment_seconds'] = time.monotonic()-started
        output.with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    check(*(Path(value) for value in sys.argv[1:]))
