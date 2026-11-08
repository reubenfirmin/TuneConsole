"""Enabled intent, disabled defaults and continuing writers share one selection."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from tests.genre_catalog_support import bound_shipped_catalog, use_shipped_catalog
from tests.store_fixture_support import numbered_store as installed
from yt_playlist.core import genre_identity
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import genre_preferences as preparation, genre_preference_publication as publication


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Selection exercises real SQL and files without HTTP startup."""


@pytest.fixture(autouse=True)
def preference_vocabulary(installed, monkeypatch):
    """Keep the shipped identities and aliases used by the original cases."""
    use_shipped_catalog(monkeypatch)
    bound_shipped_catalog(monkeypatch, ('drum bass', 'rock'))


def seed(store, *, custom=False):
    with store.conn:
        genre_identity.seed_catalog(store.conn)
        store.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            [('genres_seeded', '1'), ('genre_defaults', '[ "Drum And Bass", "Rock", "Rock", "Unknown default", "" ]')])
    store.genres.set_genres(['Drum And Bass', *(['Local deliberate style'] if custom else [])])


def prepare(conn):
    for job in (preparation.CHOICES, preparation.DEFAULTS):
        job.install(conn)
        for _ in range(100):
            if job.step(conn, batch_size=2).status == 'prepared':
                break
        else:
            pytest.fail('Genre preferences did not reconcile')


def ready(conn, size=2):
    prepare(conn)
    for _ in range(100):
        stage = publication.step(conn, batch_size=size)
        if stage.complete:
            return stage.id
    pytest.fail('Genre preferences did not stage')


def sources(conn):
    return {owner:list(map(tuple,conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+key)))
        for owner,key in (('genre_whitelist','name'),('settings','key'),('schema_migrations','version'))}


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_custom_admission_is_bounded_explicit_and_requires_reconciliation(installed):
    store, _ = installed
    seed(store, custom=True)
    prepare(store.conn)
    with pytest.raises(MigrationError,match='custom admission'):
        publication.step(store.conn)
    assert not store.conn.execute("SELECT 1 FROM genre_catalog WHERE key='local deliberate style'").fetchone()
    before = sources(store.conn)
    assert publication.admit_custom(store.conn,batch_size=1) == 1
    assert sources(store.conn) == before
    assert store.conn.execute("SELECT source FROM genre_catalog WHERE key='local deliberate style'").fetchone()[0] == 'custom'
    with pytest.raises(MigrationError,match='reconciled'):
        publication.step(store.conn)
    stage = ready(store.conn)
    publication.publish(store.conn,identity=stage)
    assert 'Local deliberate style' in store.genres.get_genre_whitelist()
    assert store.genres.enabled_matcher()['local deliberate style'] == 'Local deliberate style'
    assert publication.admit_custom(store.conn) == 0
    with pytest.raises(sqlite3.IntegrityError,match='future cleanup'):
        store.conn.execute('DELETE FROM prepared_genre_choice_originals')
    store.conn.rollback()


@pytest.mark.parametrize('installed', [84], indirect=True)
@pytest.mark.parametrize('seed_marker', ['absent',None,''])
def test_never_introduced_defaults_stay_absent(installed,seed_marker):
    store, _ = installed
    seed(store)
    store.genres.set_genres([])
    with store.conn:
        store.conn.execute("DELETE FROM settings WHERE key='genres_seeded'")
        if seed_marker != 'absent':
            store.conn.execute("INSERT INTO settings VALUES ('genres_seeded',?)",(seed_marker,))
    stage = ready(store.conn)
    publication.publish(store.conn,identity=stage)
    assert not store.conn.execute('SELECT * FROM '+publication.relation(store.conn)).fetchone()
    header = json.loads(publication.stage(store.conn,stage).baseline)
    assert header['seeded_present'] == int(seed_marker!='absent')
    assert header['seeded_value'] == (None if seed_marker=='absent' else seed_marker)
    assert store.genres.get_genre_whitelist() == []


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_ambiguity_stops_selection_and_compatible_edits_reconcile(installed):
    store, _ = installed
    seed(store)
    with store.conn:
        store.conn.execute("INSERT INTO genre_alias VALUES ('ambiguous','Drum And Bass','rock','custom')")
    prepare(store.conn)
    with pytest.raises(MigrationError,match='ambiguity repair'):
        publication.step(store.conn)
    assert publication.selected(store.conn) is None
    regular.migrate(store.conn)
    with store.conn:
        store.conn.execute("DELETE FROM genre_alias WHERE label_key='ambiguous'")
    stage = ready(store.conn)
    publication.publish(store.conn,identity=stage)
    store.genres.remove_genre('Drum And Bass')
    assert publication.selected(store.conn) is None
    assert store.genres.get_genre_whitelist() == []
    second = ready(store.conn)
    publication.publish(store.conn,identity=second)
    assert list(map(tuple,store.conn.execute('SELECT genre_key,enabled FROM '+publication.relation(store.conn)+' ORDER BY genre_key'))) == [('drum bass',0),('rock',0)]
    with store.conn:
        store.conn.execute("UPDATE genre_catalog SET name='Renamed Rock' WHERE key='rock'")
    assert publication.selected(store.conn) is None
    third = ready(store.conn)
    publication.publish(store.conn,identity=third)
    assert len({stage,second,third}) == 3


@pytest.mark.parametrize('installed', [84], indirect=True)
@pytest.mark.parametrize('damage',['choice','default','missing'])
def test_independent_verification_rejects_wrong_or_missing_preparation(installed,damage):
    store, _ = installed
    seed(store)
    prepare(store.conn)
    with store.conn:
        if damage=='choice':
            store.conn.execute("UPDATE prepared_genre_choices SET genre_key='rock'")
        elif damage=='default':
            store.conn.execute("UPDATE prepared_genre_default_items SET raw_label='Altered' WHERE position=0")
        else:
            store.conn.execute('DELETE FROM prepared_genre_default_items WHERE position=0')
    stage = publication.step(store.conn).id
    with pytest.raises(MigrationError,match='differs|missing|extra'):
        publication.publish(store.conn,identity=stage)
    assert publication.selected(store.conn) is None
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_missing_file_admission_repair_and_failed_preparation_are_independent(installed):
    store, _ = installed
    seed(store)
    stage = ready(store.conn)
    header = json.loads(publication.stage(store.conn,stage).baseline)
    Path(header['filename']).unlink()
    regular.migrate(store.conn)
    with pytest.raises(FileNotFoundError):
        publication.publish(store.conn,identity=stage)
    publication.repair_baseline(store.conn)
    publication.publish(store.conn,identity=stage)
    store.settings.set_setting('genre_defaults','null')
    with pytest.raises(MigrationError,match='list of labels'):
        preparation.DEFAULTS.step(store.conn)
    failed = preparation.DEFAULTS.progress(store.conn)
    assert failed.status=='failed' and publication.selected(store.conn) is None
    regular.migrate(store.conn)
    assert preparation.DEFAULTS.progress(store.conn)==failed
    store.settings.set_setting('genre_defaults','["Rock"]')
    preparation.DEFAULTS.step(store.conn,batch_size=2,retry=True)
    prepare(store.conn)
    second=ready(store.conn)
    publication.publish(store.conn,identity=second)
    assert second!=stage


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_interrupted_staging_and_late_write_cannot_select_stale_generation(installed,monkeypatch):
    store, _ = installed
    seed(store)
    prepare(store.conn)
    prefix=publication.step(store.conn,batch_size=1)
    class Interrupted(sqlite3.Connection):
        def execute(self,sql,parameters=()):
            if sql.startswith('UPDATE '+publication.table('stages')):
                raise KeyboardInterrupt('controlled interruption')
            return super().execute(sql,parameters)
    with sqlite3.connect(store._db_path,factory=Interrupted) as conn:
        conn.row_factory=sqlite3.Row
        with pytest.raises(KeyboardInterrupt):
            publication.step(conn,batch_size=1)
    assert publication.stage(store.conn,prefix.id)==prefix
    with sqlite3.connect(store._db_path) as conn:
        conn.row_factory=sqlite3.Row
        for _ in range(20):
            result=publication.step(conn,batch_size=1)
            if result.complete:
                break
        assert result.complete and result.id==prefix.id
    verify=publication.verify
    def late(*args):
        result=verify(*args)
        store.genres.add_genre('Rock')
        return result
    monkeypatch.setattr(publication,'verify',late)
    with pytest.raises(MigrationError,match='changed after verification|require reconciled'):
        publication.publish(store.conn,identity=result.id)
    assert publication.selected(store.conn) is None


@pytest.mark.parametrize('installed', [84], indirect=True)
def test_slow_file_verification_leaves_serving_writes_and_regular_ddl_available(installed,monkeypatch):
    store, _ = installed
    seed(store)
    stage=ready(store.conn)
    entered,release=threading.Event(),threading.Event()
    read=preparation.read_baseline
    def slow(*args):
        result=read(*args)
        entered.set()
        assert release.wait(15)
        return result
    monkeypatch.setattr(preparation,'read_baseline',slow)
    def select():
        with sqlite3.connect(store._db_path,timeout=.1) as conn:
            conn.row_factory=sqlite3.Row
            publication.publish(conn,identity=stage)
    with ThreadPoolExecutor() as pool:
        task=pool.submit(select)
        try:
            assert entered.wait(15)
            store.genres.add_genre('Rock')
            regular.migrate(store.conn)
        finally:
            release.set()
        with pytest.raises(MigrationError,match='differs|changed|require reconciled'):
            task.result(timeout=15)
    assert publication.selected(store.conn) is None
