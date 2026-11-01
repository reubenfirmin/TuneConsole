"""Measure demand publication with actual retained listening opportunity identities."""
import json
from pathlib import Path
import sqlite3
import sys

from actual_publication_timing import WriterTimings
from check_genre_publication_actual import fingerprint
from yt_playlist.core import recommendation_requests
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.preparation import recommendation_demand as preparation, demand_publication as publication
from yt_playlist.core.store import Store


def check(source, fixture, destination):
    root = Path.cwd()/'.pytest_cache'
    assert all(path.is_relative_to(root) for path in (source,fixture,destination))
    assert source!=fixture and not fixture.exists()
    fixture.parent.mkdir(parents=True,exist_ok=True)
    report = {'status':'failed','source':str(source),'fixture':str(fixture),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'dataset':'Genuine frozen v86 fixture with actual held choice windows reconstructed from native v93 demand owners. Original pre-v87 JSON spelling and chronology are unavailable. Other library data is absent; native empty Home/album demands remain absent.',
        'controls':'Read-only isolated actual-library source, no native writes, HTTP, provider calls or background workers. A same-value source update forces withdrawal and reconciliation.',
        'timing':'Explicit BEGIN IMMEDIATE reservation through commit/rollback completion.'}
    actual = sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    actual.row_factory = sqlite3.Row
    protected = {'schema_migrations':'version','listening_choice_windows':'format',
        'album_menu_refresh_state':'id','album_menu_refresh_exclusions':'position',
        'home_genre_requests':'id','home_genre_request_items':'kind,position'}
    actual.execute('BEGIN')
    before = {owner:fingerprint(actual,owner,key) for owner,key in protected.items()}
    expected = {surface:recommendation_requests.read(actual,surface) for surface in preparation.SURFACES}
    timestamps = {row['format']:row['updated_at'] for row in actual.execute('SELECT * FROM listening_choice_windows')}
    # These actual-source checks require a separate fixture with the corresponding
    # references if a future source has nonempty genre/album demand.
    assert expected['album_menu_refresh'] is None and expected['home_genre_request'] is None
    actual.rollback()
    store, timer = Store(fixture), WriterTimings()
    conn = store.conn
    try:
        runner.migrate(conn,registry=registry.MIGRATIONS[:86])
        store.init_schema(defer_metadata=True)
        with conn:
            for surface,value in expected.items():
                if value is not None:
                    conn.execute('INSERT INTO main.rec_proposals VALUES (?,?,?)',
                        (surface,json.dumps(value,ensure_ascii=False,indent=2),timestamps[surface.split(':',1)[1]]))
        sources = {'schema_migrations':fingerprint(conn,'schema_migrations','version'),
            'rec_proposals':fingerprint(conn,'rec_proposals','surface')}
        timer.watch(conn)
        def ready():
            detail = {'preparation':[],'staging':[]}
            preparation.JOB.install(conn)
            for _ in range(50):
                progress,timing = timer.timed(lambda:preparation.JOB.step(conn,batch_size=1))
                detail['preparation'].append(timing)
                if progress.status=='prepared':
                    break
            else:
                raise AssertionError('Demand preparation did not finish')
            for _ in range(50):
                stage,timing = timer.timed(lambda:publication.step(conn,batch_size=1))
                detail['staging'].append(timing)
                if stage.complete:
                    break
            else:
                raise AssertionError('Demand staging did not finish')
            _,detail['selection'] = timer.timed(lambda:publication.publish(conn,identity=stage.id))
            with store.read_view() as reader:
                reader.conn.set_authorizer(lambda operation,owner,column,*_:sqlite3.SQLITE_DENY
                    if operation==sqlite3.SQLITE_READ and owner=='rec_proposals' and column=='payload'
                    else sqlite3.SQLITE_OK)
                assert {surface:reader.rec_proposals.get_proposals(surface) for surface in expected}==expected
                assert {surface:reader.rec_proposals.has_proposals(surface) for surface in expected}=={
                    surface:bool(value) for surface,value in expected.items()}
            return stage.id,detail
        first,report['initial'] = ready()
        _,report['regular_selected'] = timer.timed(lambda:regular.migrate(conn))
        def same_value():
            conn.execute('BEGIN IMMEDIATE')
            try:
                conn.execute("UPDATE main.rec_proposals SET payload=payload WHERE surface='choice_window:albums'")
                conn.commit()
            finally:
                conn.rollback()
        _,report['continuing_writer'] = timer.timed(same_value)
        assert publication.selected(conn) is None
        assert store.rec_proposals.get_proposals('choice_window:albums')==expected['choice_window:albums']
        second,report['reconciled'] = ready()
        assert first!=second
        assert sources=={'schema_migrations':fingerprint(conn,'schema_migrations','version'),
            'rec_proposals':fingerprint(conn,'rec_proposals','surface')}
        report.update(status='passed',held_choice_windows=sum(value is not None for value in expected.values()),
            exact_native_identity_and_window=True,legacy_payload_reads_denied=True,exact_fixture_sources_and_history=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timer.close()
        report['actual_sources_exact'] = before=={owner:fingerprint(actual,owner,key) for owner,key in protected.items()}
        conn.close()
        actual.close()
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(report,indent=2)+'\n')
    assert report['actual_sources_exact']


if __name__=='__main__':
    check(*(Path(argument).resolve() for argument in sys.argv[1:]))
