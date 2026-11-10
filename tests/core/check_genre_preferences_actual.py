"""Measure preference publication with actual enabled labels in a v84 fixture."""
import json
from pathlib import Path
import sqlite3
import sys

from actual_publication_timing import WriterTimings
from check_genre_publication_actual import fingerprint
from yt_playlist.core import genre_identity
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.preparation import genre_preferences as preparation, genre_preference_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.util import genre_catalog


def check(source, fixture, destination):
    root = Path.cwd()/'.pytest_cache'
    assert all(path.is_relative_to(root) for path in (source, fixture, destination))
    assert source != fixture and not fixture.exists()
    fixture.parent.mkdir(parents=True,exist_ok=True)
    report = {'status':'failed','source':str(source),'fixture':str(fixture),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'dataset':'Genuine frozen v84 fixture with actual current enabled genre labels, catalog/aliases and retained genre-default/seed settings from the isolated v93 snapshot. This does not reconstruct exact historical pre-v85 labels or chronology; unrelated fixture data is empty.',
        'controls':'Read-only actual copy, no native access, providers, HTTP or background workers. Same-value source write forces withdrawal and a new publication.',
        'timing':'Maximum explicit BEGIN IMMEDIATE reservation through commit/rollback completion.'}
    actual = sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    actual.row_factory=sqlite3.Row
    protected={'schema_migrations':'version','settings':'key','genre_preferences':'genre_key','genre_catalog':'key','genre_alias':'label_key'}
    actual.execute('BEGIN')
    before={owner:fingerprint(actual,owner,key) for owner,key in protected.items()}
    enabled=[row[0] for row in actual.execute('SELECT c.name FROM genre_preferences p JOIN genre_catalog c ON c.key=p.genre_key WHERE enabled=1 ORDER BY p.genre_key')]
    catalog=list(map(tuple,actual.execute('SELECT key,name,family_key,source FROM genre_catalog ORDER BY key')))
    families=list(map(tuple,actual.execute('SELECT key,name FROM genre_family ORDER BY key')))
    aliases=list(map(tuple,actual.execute('SELECT * FROM genre_alias ORDER BY label_key')))
    settings=dict(actual.execute("SELECT key,value FROM settings WHERE key IN ('genre_defaults','genres_seeded')"))
    actual.rollback()
    store,timer=Store(fixture),WriterTimings()
    conn=store.conn
    try:
        runner.migrate(conn,registry=registry.MIGRATIONS[:84])
        store.init_schema(defer_metadata=True)
        with conn:
            genre_identity.seed_catalog(conn)
            conn.executemany('INSERT INTO genre_family VALUES (?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name',families)
            conn.executemany('INSERT INTO genre_catalog(key,name,family_key,source) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name,family_key=excluded.family_key,source=excluded.source',catalog)
            conn.executemany('INSERT INTO genre_alias VALUES (?,?,?,?) ON CONFLICT(label_key) DO UPDATE SET label=excluded.label,genre_key=excluded.genre_key,source=excluded.source',aliases)
            conn.executemany('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',settings.items())
        store.genres.set_genres(enabled)
        fixture_sources={owner:fingerprint(conn,owner,key) for owner,key in {'schema_migrations':'version','settings':'key','genre_whitelist':'name'}.items()}
        labels=json.loads(settings['genre_defaults']) if 'genre_defaults' in settings else genre_catalog.legacy_names()
        def resolve(label):
            keys={row[0] for row in conn.execute('SELECT key FROM genre_catalog WHERE key=? OR name=? COLLATE NOCASE UNION SELECT genre_key FROM genre_alias WHERE label_key=? OR label=? COLLATE NOCASE',
                (label,label,genre_catalog.normalize(label),label))} if label else set()
            assert len(keys)<=1, 'Actual enabled/default label has ambiguous identities: '+repr(label)
            return next(iter(keys)) if keys else None
        expected={}
        if settings.get('genres_seeded') or enabled:
            expected.update((key,0) for label in labels if (key:=resolve(label)) is not None)
        expected.update((resolve(label),1) for label in enabled)
        assert None not in expected
        report.update(enabled_sources=len(enabled),ordered_baseline_labels=len(labels),expected_preferences=len(expected))
        timer.watch(conn)
        def ready():
            detail={'preparation':[],'staging':[]}
            for job in (preparation.CHOICES,preparation.DEFAULTS):
                job.install(conn)
                for _ in range(10000):
                    progress,timing=timer.timed(lambda:job.step(conn,batch_size=16))
                    detail['preparation'].append({'job':job.name,**timing})
                    if progress.status=='prepared':
                        break
                else:
                    raise AssertionError('Actual-label preference preparation did not finish')
            for _ in range(10000):
                stage,timing=timer.timed(lambda:publication.step(conn,batch_size=16))
                detail['staging'].append(timing)
                if stage.complete:
                    break
            else:
                raise AssertionError('Actual-label preference publication did not stage')
            _,detail['selection']=timer.timed(lambda:publication.publish(conn,identity=stage.id))
            assert dict(conn.execute('SELECT genre_key,enabled FROM '+publication.relation(conn)))==expected
            return stage.id,detail
        first,report['initial']=ready()
        with store.read_view() as reader:
            reader.conn.set_authorizer(lambda operation,owner,*_:sqlite3.SQLITE_DENY
                if operation==sqlite3.SQLITE_READ and owner=='genre_whitelist' else sqlite3.SQLITE_OK)
            assert reader.genres.get_genre_whitelist()==sorted(set(enabled),key=str.lower)
            assert reader.genres.enabled_matcher()
        _,report['regular_selected']=timer.timed(lambda:regular.migrate(conn))
        def same_value():
            conn.execute('BEGIN IMMEDIATE')
            try:
                conn.execute('UPDATE main.genre_whitelist SET name=name WHERE name=(SELECT min(name) FROM main.genre_whitelist)')
                conn.commit()
            finally:
                conn.rollback()
        _,report['continuing_writer']=timer.timed(same_value)
        assert publication.selected(conn) is None
        second,report['reconciled']=ready()
        assert second!=first
        assert fixture_sources=={owner:fingerprint(conn,owner,key) for owner,key in {'schema_migrations':'version','settings':'key','genre_whitelist':'name'}.items()}
        report.update(status='passed',selected_rows_independently_matched=True,old_choice_reads_denied=True,
            exact_fixture_sources_and_history=True,continuing_write_reconciled=True)
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
