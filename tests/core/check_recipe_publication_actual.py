"""Measure selected ordered membership using the actual saved road-trip recipe."""
import json
from pathlib import Path
import sqlite3
import sys

from actual_publication_timing import WriterTimings
from check_genre_publication_actual import fingerprint
from yt_playlist.core import recipe_memberships
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.preparation import recipe_memberships as preparation, recipe_publication as publication
from yt_playlist.core.store import Store


def check(source, fixture, destination):
    root=Path.cwd()/'.pytest_cache'
    assert all(path.is_relative_to(root) for path in (source,fixture,destination))
    assert source!=fixture and not fixture.exists()
    fixture.parent.mkdir(parents=True,exist_ok=True)
    report={'status':'failed','source':str(source),'fixture':str(fixture),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'dataset':'Genuine frozen v88 fixture with actual current road-trip recipe metadata, ordered labels reconstructed from native membership, and actual artist/genre references. This does not reconstruct original pre-v89 JSON spelling or chronology. Other library data is absent.',
        'controls':'Read-only isolated v93 source; no native access, HTTP, providers or background workers. Same-value compatible write forces withdrawal and a new generation.',
        'timing':'Explicit BEGIN IMMEDIATE reservation through commit/rollback completion.'}
    actual=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    actual.row_factory=sqlite3.Row
    protected={'schema_migrations':'version','road_trip_recipes':'id','recipe_artists':'recipe_id,position',
        'recipe_genres':'recipe_id,role,position','recipe_unresolved_artists':'recipe_id,position',
        'recipe_unresolved_genres':'recipe_id,role,position','artists':'id','artist_aliases':'artist_id,alias,source',
        'artist_provider_ids':'artist_id,provider,provider_id',
        'artist_redirects':'retired_id','genre_catalog':'key','genre_alias':'label_key','genre_family':'key'}
    actual.execute('BEGIN')
    before={owner:fingerprint(actual,owner,key) for owner,key in protected.items()}
    recipes=[dict(row) for row in actual.execute('SELECT * FROM road_trip_recipes ORDER BY id')]
    expected={row['id']:recipe_memberships.read(actual,row['id']) for row in recipes}
    references={owner:list(map(tuple,actual.execute('SELECT * FROM '+owner))) for owner in
        ('artists','artist_aliases','artist_provider_ids','artist_redirects','genre_family','genre_catalog','genre_alias')}
    actual.rollback()
    store,timer=Store(fixture),WriterTimings()
    conn=store.conn
    try:
        runner.migrate(conn,registry=registry.MIGRATIONS[:88])
        store.init_schema(defer_metadata=True)
        with conn:
            for owner,rows in references.items():
                if rows:
                    upsert={'genre_family':' ON CONFLICT(key) DO UPDATE SET name=excluded.name',
                        'genre_catalog':' ON CONFLICT(key) DO UPDATE SET name=excluded.name,family_key=excluded.family_key,source=excluded.source',
                        'genre_alias':' ON CONFLICT(label_key) DO UPDATE SET label=excluded.label,genre_key=excluded.genre_key,source=excluded.source'}.get(owner,'')
                    conn.executemany('INSERT INTO '+owner+' VALUES ('+','.join('?' for _ in rows[0])+')'+upsert,rows)
            for row in recipes:
                values={**row,**{field:json.dumps(expected[row['id']][field],ensure_ascii=False,indent=2) for field in preparation.FIELDS}}
                conn.execute('INSERT INTO road_trip_recipes('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',tuple(values.values()))
        assert all(fingerprint(conn,owner,protected[owner])==before[owner] for owner in references)
        fixture_sources={owner:fingerprint(conn,owner,key) for owner,key in {'road_trip_recipes':'id','schema_migrations':'version'}.items()}
        report.update(recipes=len(recipes),ordered_members=sum(len(values) for selections in expected.values() for values in selections.values()),
            unresolved_artists=actual.execute('SELECT count(*) FROM recipe_unresolved_artists').fetchone()[0],
            unresolved_genres=actual.execute('SELECT count(*) FROM recipe_unresolved_genres').fetchone()[0])
        timer.watch(conn)
        def ready():
            detail={'preparation':[],'staging':[]}
            preparation.JOB.install(conn)
            for _ in range(10000):
                progress,timing=timer.timed(lambda:preparation.JOB.step(conn,batch_size=16))
                detail['preparation'].append(timing)
                if progress.status=='prepared':
                    break
            else:
                raise AssertionError('Recipe preparation did not finish')
            for _ in range(10000):
                stage,timing=timer.timed(lambda:publication.OWNER.step(conn,batch_size=16))
                detail['staging'].append(timing)
                if stage.complete:
                    break
            else:
                raise AssertionError('Recipe publication did not stage')
            _,detail['selection']=timer.timed(lambda:publication.OWNER.publish(conn,stage=stage.id))
            with store.read_view() as reader:
                reader.conn.set_authorizer(lambda operation,owner,column,*_:sqlite3.SQLITE_DENY
                    if operation==sqlite3.SQLITE_READ and owner=='road_trip_recipes' and column in preparation.FIELDS
                    else sqlite3.SQLITE_OK)
                selected=reader.road_trip.list_road_trip_recipes()
                assert {row['id']:{field:row[field] for field in preparation.FIELDS} for row in selected}==expected
                assert all({key:row[key] for key in source}==source for row in selected for source in recipes if row['id']==source['id'])
            return stage.id,detail
        first,report['initial']=ready()
        _,report['regular_selected']=timer.timed(lambda:regular.migrate(conn))
        if recipes:
            def same_value():
                conn.execute('BEGIN IMMEDIATE')
                try:
                    conn.execute('UPDATE main.road_trip_recipes SET genres=genres WHERE id=?',(recipes[0]['id'],))
                    conn.commit()
                finally:
                    conn.rollback()
            _,report['continuing_writer']=timer.timed(same_value)
            assert publication.OWNER.selected(conn) is None
            second,report['reconciled']=ready()
            assert second!=first
        assert fixture_sources=={owner:fingerprint(conn,owner,key) for owner,key in {'road_trip_recipes':'id','schema_migrations':'version'}.items()}
        report.update(status='passed',exact_ordered_native_membership=True,legacy_json_reads_denied=True,exact_fixture_sources_and_history=True)
    except BaseException as exc:
        report['error']=repr(exc)
        raise
    finally:
        timer.close()
        report['actual_sources_exact']=before=={owner:fingerprint(actual,owner,key) for owner,key in protected.items()}
        conn.close()
        actual.close()
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(report,indent=2)+'\n')
    assert report['actual_sources_exact']


if __name__=='__main__':
    check(*(Path(argument).resolve() for argument in sys.argv[1:]))
