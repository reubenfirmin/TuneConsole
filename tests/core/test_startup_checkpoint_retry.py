"""Real background checkpoints resume only while their source is unchanged."""
from contextlib import closing
import sqlite3
import threading

import pytest

from tests.core.test_failed_runtime_retry import installed_input
from tests.genre_catalog_support import bound_catalog
from tests.rebuild_startup_support import startup_destination
from yt_playlist.core import db_handles
from yt_playlist.core.db_rebuild import destination, source
from yt_playlist.core.storage_maintenance import StorageMaintenance
from yt_playlist.core.store import Store
from yt_playlist.rec.choice_evidence import STORAGE_PENDING


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """This background storage owner does not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('checkpoint-retry-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


def test_checkpoint_source_keeps_real_installed_history_and_populated_catalog(installed_input):
    template, version = installed_input
    assert source.admit(template) == version
    assert template.execute('PRAGMA query_only').fetchone()[0] == 1
    assert template.execute('SELECT recording_id FROM catalog_entries WHERE id=108').fetchone()[0] == 8


@pytest.fixture(scope='module', params=[
    ('production64_template', 64, False),
    ('development93_template', 93, False),
    ('development93_template', 93, True),
], ids=['production64-unchanged', 'development93-unchanged', 'development93-accepted-edit'])
def checkpoint_branch(request, startup_destination, tmp_path_factory):
    name, version, changed = request.param
    template = request.getfixturevalue(name)
    if version == 93:
        template, _seconds = template
    path = tmp_path_factory.mktemp(f'checkpoint-{version}-{changed}') / 'installed.db'
    with closing(sqlite3.connect(path)) as conn:
        template.backup(conn)
        conn.execute('PRAGMA journal_mode=WAL')
        with conn:
            conn.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (STORAGE_PENDING, '1'))
    store = Store(path)
    with pytest.MonkeyPatch.context() as patch:
        # The retry owns source identity, checkpoint custody, worker lifecycle
        # and installation. Real catalog conversion has independent owners.
        patch.setattr(destination, 'for_source', lambda path, **kwargs: startup_destination)
        patch.setattr(destination, 'signature', lambda: startup_destination.version)
        branch = dict(store=store, path=path, version=version, changed=changed,
            history=tuple(map(tuple, template.execute('SELECT * FROM schema_migrations ORDER BY version'))))
        try:
            yield branch
        finally:
            if 'release' in branch:
                branch['release'].set()
            for name in ('first', 'second'):
                if name in branch:
                    branch[name].stop()
                    branch[name].join(10)
            store.conn.close()


@pytest.fixture(scope='module')
def admitted_checkpoint(checkpoint_branch):
    checkpoint_branch['store'].init_schema(defer_metadata=True)
    return checkpoint_branch


def test_checkpoint_admission_keeps_the_source_history_and_real_wal(admitted_checkpoint):
    store = admitted_checkpoint['store']
    assert tuple(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == admitted_checkpoint['history']
    assert store.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert store.activity_paused == (admitted_checkpoint['version'] == 64)


@pytest.fixture(scope='module')
def interrupted_checkpoint(admitted_checkpoint):
    branch = admitted_checkpoint
    first = branch['first'] = StorageMaintenance(branch['store'])
    report = first._progress

    def interrupted(phase, detail):
        report(phase, detail)
        if detail.get('stage') == 'completed-runtime-input':
            raise InterruptedError('controlled background checkpoint')

    first._progress = interrupted
    first.start()
    first.join(10)
    assert first.phase == 'deferred', first.error
    branch['failed'] = db_handles.state(branch['path'])
    return branch


def test_real_background_interruption_keeps_a_building_checkpoint_and_unchanged_source(interrupted_checkpoint):
    branch = interrupted_checkpoint
    assert branch['failed']['phase'] == 'failed'
    assert branch['failed']['failed_phase'] == 'building'
    assert tuple(map(tuple, branch['store'].conn.execute(
        'SELECT * FROM schema_migrations ORDER BY version'))) == branch['history']
    assert branch['store'].activity_paused == (branch['version'] == 64)


@pytest.fixture(scope='module')
def edited_checkpoint(interrupted_checkpoint):
    if interrupted_checkpoint['changed']:
        interrupted_checkpoint['store'].settings.set_setting('accepted-between-attempts', 'preserved')
    return interrupted_checkpoint


def test_an_accepted_edit_is_readable_before_retry_and_does_not_erase_the_checkpoint(edited_checkpoint):
    branch = edited_checkpoint
    assert branch['store'].settings.get_setting('accepted-between-attempts') == (
        'preserved' if branch['changed'] else None)
    assert db_handles.state(branch['path'])['id'] == branch['failed']['id']


@pytest.fixture(scope='module')
def retried_checkpoint(edited_checkpoint):
    installed, release = threading.Event(), threading.Event()

    def runtime_resumed():
        if not installed.is_set():
            installed.set()
            assert release.wait(10)

    edited_checkpoint['release'] = release
    second = edited_checkpoint['second'] = StorageMaintenance(
        edited_checkpoint['store'], on_resume=runtime_resumed)
    second.start()
    assert installed.wait(10), second.error
    return edited_checkpoint


def test_background_retry_resumes_unchanged_sources_and_restarts_after_accepted_edits(retried_checkpoint):
    branch, second = retried_checkpoint, retried_checkpoint['second']
    if branch['changed']:
        assert second.report['id'] != branch['failed']['id']
        assert branch['store'].settings.get_setting('accepted-between-attempts') == 'preserved'
    else:
        assert second.report['id'] == branch['failed']['id'] and second.report['resumed']
    assert not second.ready.is_set() and not branch['store'].activity_paused
    assert branch['store'].conn.execute(
        'SELECT max(version) FROM database_rebuild_source_history').fetchone()[0] == branch['version']


@pytest.fixture(scope='module')
def completed_checkpoint(retried_checkpoint):
    branch = retried_checkpoint
    branch['release'].set()
    branch['second'].join(10)
    assert branch['second'].phase == 'done', branch['second'].error
    return branch


def test_completed_checkpoint_retry_accepts_a_new_real_write(completed_checkpoint):
    store = completed_checkpoint['store']
    assert completed_checkpoint['second'].ready.is_set()
    store.settings.set_setting('after-automatic-resume', 'accepted')
    assert store.settings.get_setting('after-automatic-resume') == 'accepted'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
