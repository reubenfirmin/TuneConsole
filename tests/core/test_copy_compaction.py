"""Closed-handle replacement, durable recovery and continuing installed writes."""
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import db_handles
from yt_playlist.core.db_rebuild import install, job as copy_compaction
from yt_playlist.core.db_space import StorageCompacting
from yt_playlist.core.store import Store


@pytest.mark.parametrize('installed',[79,93,107],indirect=True)
def test_closed_file_install_preserves_format_and_continuing_writes(installed,tmp_path):
    store,version = installed
    store.settings.set_setting('personal','exact retained value')
    old = store.conn
    observations = []
    def progress(phase,detail):
        if phase in {'copy','staging'}:
            assert store.settings.get_setting('personal')=='exact retained value'
            with pytest.raises(StorageCompacting):
                store.settings.set_setting('forbidden','during copy')
        if phase=='original_moved':
            with pytest.raises(sqlite3.ProgrammingError,match='closed'):
                old.execute('SELECT 1')
        observations.append(phase)
    report = copy_compaction.run(store,progress=progress)
    assert report['phase']=='complete'
    assert report['copy']['installed_version']==version
    assert Path(report['original']).exists()
    assert Path(report['backup']).exists()
    assert report['physical_reclaimed_bytes']==0
    assert 'original_moved' in observations
    assert not store.activity_paused
    assert store.conn is not old
    assert store.settings.get_setting('personal')=='exact retained value'
    store.settings.set_setting('after','accepted')
    assert store.settings.get_setting('after')=='accepted'


@pytest.mark.parametrize('installed',[79],indirect=True)
@pytest.mark.parametrize('phase',['copy','verify','staging','original_moved','installed','reopening'])
def test_failure_restores_service_and_original_writes(installed,phase):
    store,_ = installed
    store.settings.set_setting('personal','retained')
    def fail(current,detail):
        if current==phase:
            raise RuntimeError('controlled '+phase+' failure')
    with pytest.raises(RuntimeError,match='controlled'):
        copy_compaction.run(store,progress=fail)
    assert db_handles.state(store._db_path)['phase']=='failed'
    assert not store.activity_paused
    assert store.settings.get_setting('personal')=='retained'
    store.settings.set_setting('resumed','yes')


@pytest.mark.parametrize('installed',[79],indirect=True)
@pytest.mark.parametrize('after_publication',[False, True])
def test_initial_state_failure_restores_write_admission(installed,monkeypatch,after_publication):
    store,_ = installed
    save = install.save
    failed = False
    def interrupted(database,state):
        nonlocal failed
        if not failed:
            failed = True
            if after_publication:
                save(database,state)
            raise OSError('controlled initial state failure')
        return save(database,state)
    monkeypatch.setattr(install,'save',interrupted)
    with pytest.raises(OSError,match='controlled initial state'):
        copy_compaction.run(store)
    assert not store.activity_paused
    assert db_handles.state(store._db_path)['phase']=='failed'
    store.settings.set_setting('after initialization failure','accepted')


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_unpersistable_failure_keeps_maintenance_until_recovery(installed,monkeypatch):
    store,_ = installed
    save = install.save
    first = True
    def unavailable(database,state):
        nonlocal first
        if first:
            first = False
            save(database,state)
        raise OSError('controlled state filesystem unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(install,'save',unavailable)
        with pytest.raises(OSError,match='state filesystem unavailable'):
            copy_compaction.run(store)
    assert store.activity_paused
    assert db_handles.state(store._db_path)['phase']=='building'
    with pytest.raises(StorageCompacting):
        store.settings.set_setting('not admitted','until recovered')
    store.conn.close()
    install.recover(store._db_path)
    install.reopen(store)
    store.activity_paused = False
    store.settings.set_setting('after recovery','accepted')


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_pinned_handle_blocks_replacement_and_same_file_reopens(installed):
    store,_ = installed
    before = store._db_path.stat().st_ino
    with closing(db_handles.connect(store._db_path)) as pinned:
        cursor = pinned.execute('SELECT name FROM sqlite_schema')
        with pytest.raises(TimeoutError,match='handles are still open'):
            copy_compaction.run(store)
        assert store._db_path.stat().st_ino==before
        assert cursor.fetchone() is not None
        assert not store.activity_paused
        store.settings.set_setting('resumed','yes')


class ProcessDied(BaseException):
    pass


@pytest.mark.parametrize('installed',[79],indirect=True)
@pytest.mark.parametrize('phase',['copy','staging','original_moved','installed','reopening'])
def test_restart_selects_original_or_verified_install(installed,phase):
    store,_ = installed
    store.settings.set_setting('personal','retained')
    def die(current,detail):
        if current==phase:
            raise ProcessDied()
    with pytest.raises(ProcessDied):
        copy_compaction.run(store,progress=die)
    store.conn.close()
    # Simulate a fresh process entering Store before opening any SQLite handle.
    recovered = Store(store._db_path)
    try:
        recovered.init_schema(defer_metadata=True)
        assert db_handles.state(store._db_path)['phase']==('complete' if phase=='installed' else 'failed')
        assert recovered.settings.get_setting('personal')=='retained'
        recovered.settings.set_setting('after restart','accepted')
    finally:
        recovered.conn.close()
    # The installed fixture also checks its original Store after yielding.
    store.conn = db_handles.connect(store._db_path)


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_other_connections_obey_read_only_admission_and_closed_cursors(installed):
    store,_ = installed
    with closing(db_handles.connect(store._db_path)) as other:
        cursor = other.cursor()
        database = store._db_path
        install.save(database,dict(format=1,database=str(database),phase='building'))
        assert other.execute('SELECT 1').fetchone()==(1,)
        with pytest.raises(StorageCompacting):
            cursor.execute("INSERT INTO settings VALUES('forbidden','write')")
        install.save(database,dict(format=1,database=str(database),phase='closing'))
        with pytest.raises(StorageCompacting):
            other.execute('SELECT 1')
        install.save(database,dict(format=1,database=str(database),phase='failed'))
        with other:
            other.execute("INSERT INTO settings VALUES('resumed','accepted')")
    # Repeated close must release exactly once, including retained cursor objects.
    other.close()
    with pytest.raises(TimeoutError):
        with db_handles.lease(database,exclusive=True,timeout=0):
            pytest.fail('the serving Store must still own its shared lifetime lease')


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_admitted_readonly_snapshot_finishes_before_closed_file_replacement(installed,monkeypatch):
    store,_ = installed
    store.settings.set_setting('bounded reader','exact retained value')
    ready, closing_handles = threading.Event(), threading.Event()
    def read():
        with closing(db_handles.connect(store._db_path.as_uri()+'?mode=ro',uri=True)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT 1 FROM settings LIMIT 1').fetchone()
            ready.set()
            assert closing_handles.wait(15)
            assert db_handles.state(store._db_path)['phase']=='closing'
            return reader.execute("SELECT value FROM settings WHERE key='bounded reader'").fetchone()[0]
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(read)
        assert ready.wait(5)
        original_lease = db_handles.lease
        @contextmanager
        def lease(path,**kwargs):
            if kwargs.get('exclusive'):
                closing_handles.set()
            with original_lease(path,**kwargs):
                yield
        monkeypatch.setattr(db_handles,'lease',lease)
        try:
            result = copy_compaction.run(store)
        finally:
            closing_handles.set()
        assert future.result(timeout=5)=='exact retained value'
    assert result['phase']=='complete'


@pytest.mark.parametrize('installed',[79],indirect=True)
@pytest.mark.parametrize('kind',['home','listening','documents','models','reports'])
def test_installed_preparation_reuses_fenced_file_custody(installed,kind,monkeypatch):
    from tests.core.test_preparation_file_recovery import selected_owner,hashes,sql_custody
    from yt_playlist.core import policy_documents, preparation_files
    store,_ = installed
    root,read,selected = selected_owner(store,kind)
    expected,selection,custody = read(store),selected(store.conn),sql_custody(store.conn)
    before_files = hashes(root)
    before_references = list(preparation_files.references(store.conn))
    def unexpected_restore(*args,**kwargs):
        pytest.fail('In-place SQL replacement must not relocate installed files')
    monkeypatch.setattr(copy_compaction.policy_recovery,'_restore_files',unexpected_restore)
    observed = []
    def progress(phase,detail):
        if phase in {'copy','staging','original_moved'}:
            with (root/'.readers.lock').open('a+b') as lock:
                assert policy_documents._try_file_lock(lock,exclusive=True) is None
            observed.append(phase)
    report = copy_compaction.run(store,progress=progress)
    assert report['source_files']['files']>0
    assert {'copy','staging','original_moved'}<=set(observed)
    assert read(store)==expected
    assert selected(store.conn)==selection
    assert sql_custody(store.conn)==custody
    assert list(preparation_files.references(store.conn))==before_references
    assert hashes(root)==before_files
    with (root/'.readers.lock').open('a+b') as lock:
        unlock = policy_documents._try_file_lock(lock,exclusive=True)
        assert unlock is not None
        unlock()


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_cross_filesystem_stages_a_verified_sibling_before_rename(installed,tmp_path,monkeypatch):
    store,_ = installed
    scratch = tmp_path/'scratch'
    scratch.mkdir()
    monkeypatch.setattr(copy_compaction,'_same_filesystem',lambda left,right:False)
    copied = []
    original = copy_compaction.shutil.copyfile
    def copy(source,target):
        assert Path(target).parent.parent==store._db_path.parent
        copied.append((source,target))
        return original(source,target)
    monkeypatch.setattr(copy_compaction.shutil,'copyfile',copy)
    # Force the branch independently of this fixture's real mount topology.
    report = copy_compaction.run(store,scratch=scratch)
    assert len(copied)==1
    assert not report['capacity']['same_filesystem']
    assert Path(report['work']).parent==scratch
    assert Path(report['original']).exists()


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_different_mount_scratch_keeps_installed_custody_on_destination(installed):
    from tests.core.test_preparation_file_recovery import selected_owner

    store,_ = installed
    with TemporaryDirectory(prefix='.test-compaction-scratch-',dir=Path.cwd()) as directory:
        scratch = Path(directory)
        if scratch.stat().st_dev==store._db_path.parent.stat().st_dev:
            pytest.skip('This machine has no distinct scratch mount for the fixture')
        producer,read,_ = selected_owner(store,'home')
        expected = read(store)
        store.settings.set_setting('cross mount','retained')
        report = copy_compaction.run(store,scratch=scratch)
        assert report['phase']=='complete'
        assert not report['capacity']['same_filesystem']
        assert Path(report['backup']).is_relative_to(scratch)
        assert Path(report['destination']).stat().st_dev==store._db_path.stat().st_dev
        assert Path(report['work']).stat().st_dev!=store._db_path.stat().st_dev
        assert store.settings.get_setting('cross mount')=='retained'
        store.settings.set_setting('after cross mount','accepted')
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    # Only scratch is disposable: the existing installed file trees stay live.
    assert producer.exists()
    assert read(store)==expected
    assert copy_compaction._owned_files(store.conn)['files']>0


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_capacity_failure_restores_admission_without_allocating_a_copy(installed,monkeypatch):
    from yt_playlist.core import db_space
    store,_ = installed
    def full(*args,**kwargs):
        raise db_space.LowDiskSpace('controlled destination capacity shortage')
    monkeypatch.setattr(db_space,'require_space',full)
    with pytest.raises(db_space.LowDiskSpace):
        copy_compaction.run(store)
    report = db_handles.state(store._db_path)
    assert report['phase']=='failed'
    assert not Path(report['destination']).exists()
    assert not store.activity_paused
    store.settings.set_setting('after space failure','retained')


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_existing_external_writer_rejects_copy_and_preserves_its_commit(installed):
    store,_ = installed
    with closing(sqlite3.connect(store._db_path)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("INSERT INTO settings VALUES('external accepted','retained')")
        with pytest.raises(sqlite3.OperationalError,match='locked'):
            copy_compaction.run(store)
        writer.commit()
    assert store.settings.get_setting('external accepted')=='retained'
    store.settings.set_setting('resumed','accepted')


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_released_pre_swap_snapshot_cannot_publish_against_reopened_database(installed):
    store,_ = installed
    with store.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting('input')
        snapshot.release_reader()
        copy_compaction.run(store)
        assert not snapshot.publish(lambda:pytest.fail('old snapshot published after database replacement'))


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_failed_internal_reopen_rolls_back_before_any_writer_is_admitted(installed,monkeypatch):
    store,_ = installed
    store.settings.set_setting('personal','retained')
    original = store._bind_repositories
    attempts = 0
    def bind():
        nonlocal attempts
        attempts += 1
        if attempts==1:
            with pytest.raises(StorageCompacting):
                db_handles.connect(store._db_path)
            raise RuntimeError('controlled repository reopen failure')
        original()
    monkeypatch.setattr(store,'_bind_repositories',bind)
    with pytest.raises(RuntimeError,match='controlled repository reopen'):
        copy_compaction.run(store)
    assert attempts==2
    assert not store.activity_paused
    assert db_handles.state(store._db_path)['phase']=='failed'
    store.settings.set_setting('resumed','accepted')
    assert store.settings.get_setting('personal')=='retained'
