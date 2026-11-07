"""Check staged taste folds against actual retained pre-v88 contributions."""
import json
from pathlib import Path
import sqlite3
import sys

from actual_publication_timing import WriterTimings
from check_genre_publication_actual import fingerprint
from yt_playlist.core import taste_axes
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.preparation import taste_axes as preparation, taste_publication as publication
from yt_playlist.core.store import Store


COLUMNS = {
    'rec_weights':'axis,weight,updated_at','rec_lean':'axis,value,updated_at,last_graduated_day',
    'home_hidden_facet':'axis','rec_theme':'facet,score,updated_at',
    'rec_play_grad':'axis,last_graduated_day',
    'rec_grad_log':'id,axis,source,score,factor,new_weight,created_at',
}


def projected(conn):
    return {owner:list(map(tuple,conn.execute(taste_axes.projection(conn,taste_axes.table(conn,owner),columns,
        token_name='facet' if owner=='rec_theme' else 'axis')+' ORDER BY 1'))) for owner,columns in COLUMNS.items()}


def check(source,fixture,destination):
    root = Path.cwd()/'.pytest_cache'
    assert all(path.is_relative_to(root) for path in (source,fixture,destination))
    native_path = fixture.with_suffix('.native88.db')
    assert source!=fixture and not fixture.exists() and not native_path.exists()
    fixture.parent.mkdir(parents=True,exist_ok=True)
    report = {'status':'failed','source':str(source),'fixture':str(fixture),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'dataset':'Actual pre-v88 contributions retained in the isolated v93 library taste_axis_legacy archive, reconstructed into a genuine frozen v87 layout with current actual genre references. These are archived historical values, not the latest operational taste state. Unrelated library data is absent.',
        'controls':'Read-only actual source; isolated v87 serving fixture and independent frozen v88 reference conversion. No native writes, provider calls, HTTP or background workers. Same-value compatible writes exercise withdrawal/reconciliation.',
        'timing':'Explicit BEGIN IMMEDIATE reservation through commit/rollback completion.'}
    actual = sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    actual.row_factory = sqlite3.Row
    protected = {'schema_migrations':'version','taste_axis_legacy':'table_name,row_key','taste_axes':'id',
        'genre_family':'key','genre_catalog':'key','genre_alias':'label_key'}
    actual.execute('BEGIN')
    before = {owner:fingerprint(actual,owner,key) for owner,key in protected.items()}
    originals = [dict(row) for row in actual.execute('SELECT * FROM taste_axis_legacy ORDER BY table_name,row_key')]
    references = {owner:[dict(row) for row in actual.execute('SELECT * FROM '+owner)] for owner in
        ('genre_family','genre_catalog','genre_alias')}
    actual.rollback()
    store,native,timer = Store(fixture),Store(native_path),WriterTimings()
    conn = store.conn
    try:
        runner.migrate(conn,registry=registry.MIGRATIONS[:87])
        store.init_schema(defer_metadata=True)
        with conn:
            for owner,rows in references.items():
                if rows:
                    key = 'label_key' if owner=='genre_alias' else 'key'
                    columns = tuple(rows[0])
                    conn.executemany('INSERT INTO '+owner+'('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+
                        ') ON CONFLICT('+key+') DO UPDATE SET '+','.join(name+'=excluded.'+name for name in columns if name!=key),
                        [tuple(row.values()) for row in rows])
            for row in originals:
                owner = row['table_name']
                values = {**row,'axis':row['raw_axis'],'facet':row['raw_axis'],'value':row['lean_value'],
                    'id':int(row['row_key']) if owner=='rec_grad_log' else None}
                columns = COLUMNS[owner].split(',')
                conn.execute('INSERT INTO '+owner+'('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                             tuple(values[column] for column in columns))
        assert all(fingerprint(conn,owner,protected[owner])==before[owner] for owner in references)
        source_owners = {**{owner:','.join(keys) for owner,keys in preparation.KEYS.items()},'schema_migrations':'version'}
        fixture_sources = {owner:fingerprint(conn,owner,key) for owner,key in source_owners.items()}
        conn.backup(native.conn)
        runner.migrate(native.conn,registry=registry.MIGRATIONS[:88])
        expected = projected(native.conn)
        timer.watch(conn)
        def ready():
            detail = {'preparation':[],'staging':[]}
            _,detail['installation'] = timer.timed(lambda:preparation.JOB.install(conn))
            for _ in range(10000):
                progress,timing = timer.timed(lambda:preparation.JOB.step(conn,batch_size=16))
                detail['preparation'].append(timing)
                if progress.status=='prepared':
                    break
            else:
                raise AssertionError('Taste preparation did not finish')
            for _ in range(10000):
                current,timing = timer.timed(lambda:publication.step(conn,batch_size=16))
                detail['staging'].append(timing)
                if current.complete:
                    break
            else:
                raise AssertionError('Taste staging did not finish')
            _,detail['selection'] = timer.timed(lambda:publication.publish(conn,identity=current.id))
            with store.read_view() as reader:
                reader.conn.set_authorizer(lambda operation,owner,column,*_:sqlite3.SQLITE_DENY
                    if operation==sqlite3.SQLITE_READ and owner in preparation.KEYS else sqlite3.SQLITE_OK)
                selected = projected(reader.conn)
                assert selected==expected
                for axis,weight,at in expected['rec_weights']:
                    assert reader.rec_model.weight_records()[axis]['weight']==weight
                assert reader.rec_signals.graduation_log_counts()=={
                    kind:sum(row[2]==kind for row in expected['rec_grad_log']) for kind in {row[2] for row in expected['rec_grad_log']}}
            return current.id,detail
        first,report['initial'] = ready()
        _,report['regular_selected'] = timer.timed(lambda:regular.migrate(conn))
        token,weight,at = next(row for row in expected['rec_weights'] if row[2] is not None)
        _,report['continuing_weight'] = timer.timed(lambda:store.rec_model.set_weight(token,weight,lo=weight,hi=weight,now=at))
        assert publication.selected(conn) is None
        _,report['withdrawn_source_projection'] = timer.timed(lambda:projected(conn))
        assert projected(conn)==expected
        assert fixture_sources=={owner:fingerprint(conn,owner,key) for owner,key in source_owners.items()}
        # Discounting zero is another exact-value edit and exercises canonical
        # lookup plus an aggregate read on a populated physical theme owner.
        theme = expected['rec_theme'][0][0]
        _,report['continuing_theme'] = timer.timed(lambda:store.rec_signals.discount_theme(theme,0))
        value,report['theme_lookup'] = timer.timed(lambda:store.rec_signals.get_theme(theme))
        assert value==expected['rec_theme'][0][1]
        second,report['reconciled'] = ready()
        assert first!=second
        assert fixture_sources=={owner:fingerprint(conn,owner,key) for owner,key in source_owners.items()}
        report.update(status='passed',archived_contributions=len(originals),
            source_counts={owner:conn.execute('SELECT count(*) FROM '+owner).fetchone()[0] for owner in COLUMNS},
            destination_counts={owner:len(values) for owner,values in expected.items()},
            exact_native_folds=True,source_reads_denied=True,exact_fixture_sources_and_history=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timer.close()
        report['actual_sources_exact'] = before=={owner:fingerprint(actual,owner,key) for owner,key in protected.items()}
        conn.close()
        native.conn.close()
        actual.close()
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(report,indent=2)+'\n')
    assert report['actual_sources_exact']


if __name__=='__main__':
    check(*(Path(argument).resolve() for argument in sys.argv[1:]))
