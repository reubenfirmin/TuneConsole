"""Check canonical subjects against independent folds of an isolated real library."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from actual_publication_timing import WriterTimings
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from tests.core.check_home_publication_actual import fingerprint
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import listening_subjects as preparation, subject_publication as publication
from yt_playlist.core.store import Store


def expected_summaries(conn):
    """Fold installed evidence in Python, independently of publication SQL."""
    recordings = {}
    for account, recording, timestamp, day, event in conn.execute('''SELECT p.identity_id,p.recording_id,p.played_at,c.day,p.id
            FROM main.play_events p JOIN main.play_event_calendar c ON c.event_id=p.id
            WHERE p.recording_id IS NOT NULL AND coalesce(p.is_generated,0)=0
            AND EXISTS(SELECT 1 FROM main.play_event_sources s WHERE s.event_id=p.id)
            ORDER BY p.played_at,p.id'''):
        result = recordings.setdefault((account, recording), [None, None, None, None])
        if result[0] is None:
            result[:2] = timestamp, day
    for account, recording, day, precision in conn.execute('''SELECT identity_id,recording_id,day,precision
            FROM main.eligible_listening_evidence WHERE recording_id IS NOT NULL AND day IS NOT NULL'''):
        result = recordings.setdefault((account, recording), [None, None, None, None])
        if precision in ('calendar_day', 'legacy_unknown'):
            column = 2 if precision == 'calendar_day' else 3
            result[column] = min(result[column], day) if result[column] else day
    credits = {}
    for recording, artist in conn.execute('''SELECT DISTINCT e.recording_id,coalesce(r.artist_id,c.artist_id)
            FROM main.catalog_entries e JOIN main.entry_artists c ON c.entry_id=e.id
            LEFT JOIN main.artist_redirects r ON r.retired_id=c.artist_id'''):
        credits.setdefault(recording, set()).add(artist)
    artists = {}
    for (account, recording), row in sorted(recordings.items()):
        for artist in credits.get(recording, ()):
            result = artists.setdefault((account, artist), [None, None, None, None])
            if row[0] is not None and (result[0] is None or row[0]<result[0]):
                result[:2] = row[:2]
            for column in (2, 3):
                if row[column] is not None:
                    result[column] = min(result[column], row[column]) if result[column] else row[column]
    return {'recording_listening_summary':recordings, 'artist_listening_summary':artists}


def expected_first_play(conn, summaries):
    keys = dict(conn.execute('SELECT id,identity_key FROM main.recordings'))
    result = {}
    for kind, owner in (('track', 'recording_listening_summary'), ('artist', 'artist_listening_summary')):
        for (_, subject), (timestamp, exact, observed, legacy) in summaries[owner].items():
            days = [day for day in (exact, observed, legacy) if day is not None]
            if not days:
                continue
            key = (kind, keys[subject] if kind == 'track' else '@artist:'+str(subject))
            day = (dt.date.fromisoformat(min(days))-dt.date(1970, 1, 1)).days
            old = result.get(key)
            result[key] = (min(old[0], day) if old else day,
                min(at for at in (old[1] if old else None, timestamp) if at is not None)
                if timestamp is not None or old and old[1] is not None else None, 'canonical_listening')
    rows = [(*key, *value) for key, value in result.items()]
    for key, day, timestamp in conn.execute('''SELECT identity_key,min(day),
            min(CASE WHEN precision='exact_timestamp' THEN at END) FROM main.listening_activity
            WHERE recording_id IS NULL AND day IS NOT NULL GROUP BY identity_key'''):
        rows.append(('track', key, (dt.date.fromisoformat(day)-dt.date(1970, 1, 1)).days, timestamp, 'unresolved_listening'))
    return rows


def check(database, report_path, *, refresh=False):
    workspace = Path.cwd()/'.pytest_cache'
    assert database.resolve().is_relative_to(workspace) and report_path.resolve().is_relative_to(workspace)
    assert database.name.startswith('tuneconsole-') and database.exists()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'status':'failed', 'database':str(database), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'The isolated genuine v93 actual-library snapshot; no native database access or controlled listening rows.',
        'limits':'Canonical publication, retained evidence and read projections only. No providers, native HTTP, complete preparation matrix, or production convergence claim.',
        'preparation_batches':[], 'publication_batches':[]}
    protected = ('schema_migrations', 'settings', 'play_events', 'listening_observations', 'play_event_sources',
        'play_event_calendar', 'listening_capture_receipts', 'listening_calendars', 'recordings', 'entry_artists',
        'catalog_entries', 'artist_redirects', 'recording_redirects')
    reader, store = Store(database), Store(database)
    conn = sqlite3.connect(database, timeout=.1, factory=DiagnosticConnection)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    before = {owner:fingerprint(conn, owner) for owner in protected}
    expected = expected_summaries(conn)
    expected_first = expected_first_play(conn, expected)
    timings = WriterTimings(field='writer_seconds')
    timed = timings.timed
    report['timing'] = 'Maximum writer window from BEGIN IMMEDIATE through commit/rollback completion.'
    try:
        reader.init_schema(defer_metadata=True)
        store.init_schema(defer_metadata=True)
        timings.watch(conn)
        preparation.install(conn)
        if refresh:
            with conn:
                conn.execute('UPDATE main.play_events SET played_at=played_at WHERE id=(SELECT min(id) FROM main.play_events)')
            report['refresh_control'] = 'Same-value write to one actual play event; source fingerprint unchanged; a new immutable publication is staged.'
        for _ in range(10000):
            progress, duration = timed(lambda:preparation.step(conn, batch_size=32))
            report['preparation_batches'].append(duration)
            if progress.status == 'prepared':
                break
        else:
            raise AssertionError('Actual listening preparation did not finish')
        for _ in range(10000):
            current, duration = timed(lambda:publication.step(conn, batch_size=32))
            report['publication_batches'].append({'owner':current.owner, **duration})
            if current.complete:
                break
        else:
            raise AssertionError('Actual listening publication did not finish')
        _, report['selection'] = timed(lambda:publication.publish(conn, identity=current.id))
        assert publication.selected(conn) == current.id
        for owner, expected_rows in expected.items():
            columns = ','.join((*publication.KEYS[owner], 'first_exact_play_at', 'first_exact_day',
                'first_observed_day', 'first_legacy_unknown_day'))
            rows = {tuple(row[:2]):list(row[2:]) for row in conn.execute('SELECT '+columns+' FROM '+publication.table(owner)+' WHERE stage=?', (current.id,))}
            assert rows == expected_rows, (owner, len(rows), len(expected_rows))
        expected_map = {row[1]:row[2] for row in expected_first if row[0] == 'track'}
        assert reader.trends.first_play_map('track') == expected_map
        with store.read_view() as view:
            actual_first = sorted(map(tuple, view.conn.execute('SELECT * FROM trend_first_play')))
            assert actual_first == sorted(expected_first)
            for owner in ('listening_evidence', 'eligible_listening_evidence', 'listening_days', 'listening_activity'):
                expected_rows = sorted(map(tuple, conn.execute('SELECT * FROM main.'+owner)), key=repr)
                actual_rows = sorted(map(tuple, view.conn.execute('SELECT * FROM '+owner)), key=repr)
                assert actual_rows == expected_rows, owner
        _, report['regular_admission_selected'] = timed(lambda:regular.migrate(conn))
        report.update(status='passed', selected=current.id,
            input_counts={owner:value[0] for owner, value in before.items()},
            summary_counts={owner:len(rows) for owner, rows in expected.items()},
            unresolved_events=conn.execute('SELECT count(*) FROM play_events WHERE recording_id IS NULL').fetchone()[0],
            unresolved_observations=conn.execute('SELECT count(*) FROM listening_observations WHERE recording_id IS NULL').fetchone()[0],
            first_play_rows=len(expected_first), independent_python_summary_match=True, preopened_reader_match=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timings.close()
        report['sources_exact'] = before == {owner:fingerprint(conn, owner) for owner in protected}
        reader.conn.close()
        store.conn.close()
        conn.close()
        report_path.write_text(json.dumps(report, indent=2)+'\n')
    assert report['sources_exact']
    return report


if __name__ == '__main__':
    result = check(*(Path(argument).resolve() for argument in sys.argv[1:3]), refresh='--refresh' in sys.argv[3:])
    print(json.dumps({key:result[key] for key in ('status', 'summary_counts', 'selection', 'sources_exact')}))
