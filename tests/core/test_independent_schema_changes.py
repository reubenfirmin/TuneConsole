"""Regular DDL and truthful legacy conversion history have separate commits."""
from contextlib import ExitStack, closing
import sqlite3
import threading

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.independent_schema_support import independent_regular_ddl
from tests.store_fixture_support import install_numbered_input, install_regular_input
from yt_playlist.core.migrations import MigrationError, migrate, registry, regular
from yt_playlist.core.migrations import v080_metadata_evidence
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.migrations.__main__ import main


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL and command-line checks do not construct an HTTP app."""


@pytest.fixture
def installed(tmp_path, monkeypatch):
    # These cases own regular DDL and admission over an exact v79 input.
    # Historical construction is covered by the frozen migration owners.
    bound_catalog(monkeypatch)
    path = tmp_path / 'old-format.db'
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        install_numbered_input(conn, 79)
        with conn:
            conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (3,'Listener','credential',1)")
            conn.execute("INSERT INTO settings VALUES ('retained-setting','original')")
    finally:
        conn.close()
    return path


def legacy_history(conn):
    return list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))


def test_album_feedback_outbox_installation_rolls_back_and_retries_without_advancing_layout(installed, monkeypatch):
    from yt_playlist.core import album_feedback_outbox
    with closing(sqlite3.connect(installed)) as conn:
        version = next(change.version for change in regular.CHANGES if change.name == 'durable_album_feedback_responses')
        with conn:
            install_regular_input(conn, regular.CHANGES[:version - 1])
        monkeypatch.setattr(regular, 'CHANGES', regular.CHANGES[:version])
        history = legacy_history(conn)
        before = list(conn.iterdump())
        install = album_feedback_outbox.install
        def interrupted(*args):
            install(*args)
            raise OSError('controlled outbox installation interruption')
        with monkeypatch.context() as stopped:
            stopped.setattr(album_feedback_outbox, 'install', interrupted)
            with pytest.raises(OSError, match='controlled outbox installation interruption'):
                regular.migrate(conn)
        assert list(conn.iterdump()) == before
        assert legacy_history(conn) == history
        regular.migrate(conn)
        album_feedback_outbox.validate(conn)
        assert legacy_history(conn) == history
        assert conn.execute('SELECT name FROM schema_changes WHERE version=?', (version,)).fetchone() == (
            'durable_album_feedback_responses',)


def test_wrong_album_feedback_outbox_is_rejected_without_stamping_change(installed, monkeypatch):
    with closing(sqlite3.connect(installed)) as conn:
        version = next(change.version for change in regular.CHANGES if change.name == 'durable_album_feedback_responses')
        with conn:
            install_regular_input(conn, regular.CHANGES[:version - 1])
        monkeypatch.setattr(regular, 'CHANGES', regular.CHANGES[:version])
        with conn:
            conn.execute('CREATE TABLE album_feedback_response_outbox(action_id TEXT)')
        before = list(conn.iterdump())
        with pytest.raises(MigrationError, match='album feedback response outbox'):
            regular.migrate(conn)
        assert list(conn.iterdump()) == before
        assert not conn.execute('SELECT 1 FROM schema_changes WHERE name=?',
                                ('durable_album_feedback_responses',)).fetchone()


def test_association_checkpoint_installation_rolls_back_and_retries(installed, monkeypatch):
    from yt_playlist.core import association_work
    with closing(sqlite3.connect(installed)) as conn:
        with conn:
            install_regular_input(conn, regular.CHANGES[:34])
        before = list(conn.iterdump())
        install = association_work.install

        def interrupted(*args):
            install(*args)
            raise OSError('interrupted association checkpoint installation')

        with monkeypatch.context() as stopped:
            stopped.setattr(association_work, 'install', interrupted)
            with pytest.raises(OSError, match='interrupted association checkpoint'):
                regular.migrate(conn)
        assert list(conn.iterdump()) == before
        regular.migrate(conn)
        association_work.validate(conn)
        completed = list(conn.iterdump())
        regular.migrate(conn)
        assert list(conn.iterdump()) == completed


def test_wrong_association_checkpoint_is_rejected_without_stamping(installed):
    with closing(sqlite3.connect(installed)) as conn:
        with conn:
            install_regular_input(conn, regular.CHANGES[:34])
        with conn:
            conn.execute('CREATE TABLE recording_association_work(video_id TEXT)')
        before = list(conn.iterdump())
        with pytest.raises(MigrationError, match='recording association checkpoints'):
            regular.migrate(conn)
        assert list(conn.iterdump()) == before


def test_routine_runner_never_starts_real_metadata_conversion(installed, monkeypatch):
    # Only the real receipt change is relevant to v81 adoption below. The
    # complete regular registry has separate startup/CLI owners.
    monkeypatch.setattr(regular, 'CHANGES', regular.CHANGES[:1])
    def failed_conversion(conn):
        raise RuntimeError('Conversion deliberately suspended for retry')

    with closing(sqlite3.connect(installed)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        before = legacy_history(conn)
        with monkeypatch.context() as held:
            held.setattr(v080_metadata_evidence, 'convert_legacy', failed_conversion)
            assert migrate(conn) is True
        assert legacy_history(conn) == before
        assert conn.execute('SELECT version,name FROM schema_changes').fetchall()[0][:] == (
            1, 'independent_playback_feedback_receipts')
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='metadata_observations'").fetchone()
        assert conn.execute("SELECT value FROM settings WHERE key='retained-setting'").fetchone()[0] == 'original'
        # Exercise the real independent owner while metadata remains at v79.
        with conn:
            conn.execute("INSERT INTO playback_feedback_receipts VALUES ('retained-capture',3,'live',123,1)")
        # Resuming the genuine frozen prefix must adopt that owner rather than
        # duplicate it, lose its rows or stamp the unfinished v80 early.
        migrate(conn, registry=registry.MIGRATIONS[:81])
        validate_version(conn, 81)
        assert legacy_history(conn)[:79] == before
        assert conn.execute("SELECT result FROM playback_feedback_receipts WHERE capture_id='retained-capture'").fetchone()[0] == 1


def test_regular_failure_keeps_prior_commit_and_never_changes_legacy_history(installed, independent_regular_ddl):
    def interrupted(conn, context):
        conn.execute('CREATE TABLE interrupted_owner(id INTEGER PRIMARY KEY)')
        raise RuntimeError('Interrupted regular DDL')

    with closing(sqlite3.connect(installed)) as conn, conn:
        before = legacy_history(conn)
        next_version = len(regular.CHANGES) + 1
        changes = regular.CHANGES + (regular.Change(next_version, 'later_addition', interrupted, lambda conn: None),)
        with pytest.raises(RuntimeError, match='Interrupted regular DDL'):
            regular.migrate(conn, changes=changes)
        assert legacy_history(conn) == before
        assert conn.execute('SELECT version FROM schema_changes ORDER BY version').fetchall() == [(c.version,) for c in regular.CHANGES]
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='interrupted_owner'").fetchone()
        successor = regular.Change(next_version, 'later_addition',
            lambda conn, context: conn.execute('CREATE TABLE later_owner(id INTEGER PRIMARY KEY)'),
            lambda conn: conn.execute('SELECT id FROM later_owner LIMIT 0'))
        regular.migrate(conn, changes=regular.CHANGES + (successor,))
        assert conn.execute('SELECT version FROM schema_changes ORDER BY version').fetchall() == [(c.version,) for c in regular.CHANGES]+[(next_version,)]
        assert legacy_history(conn) == before


def test_wrong_preexisting_independent_owner_is_rejected_without_stamping(installed):
    with closing(sqlite3.connect(installed)) as conn, conn:
        conn.execute('CREATE TABLE playback_feedback_receipts(capture_id TEXT)')
        before = list(conn.iterdump())
        with pytest.raises(MigrationError, match='receipt owner'):
            regular.migrate(conn)
        assert list(conn.iterdump()) == before


def test_regular_runner_preserves_caller_transaction(installed):
    with closing(sqlite3.connect(installed)) as conn, conn:
        conn.execute("UPDATE settings SET value='uncommitted' WHERE key='retained-setting'")
        with pytest.raises(MigrationError, match='outstanding caller transaction'):
            regular.migrate(conn)
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT value FROM settings WHERE key='retained-setting'").fetchone() == ('original',)


def test_regular_cli_does_not_run_legacy_conversion(installed, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('Regular schema command invoked bulk conversion')

    monkeypatch.setattr(v080_metadata_evidence, 'convert_legacy', forbidden)
    # Command routing owns DDL admission independently of bulk conversion.
    # Registry-wide installation is covered by current-schema bootstrap tests.
    monkeypatch.setattr(regular, 'CHANGES', regular.CHANGES[:1])
    assert main(['upgrade-schema', str(installed)]) == 0
    assert 'legacy layout remains 79' in capsys.readouterr().out
    with closing(sqlite3.connect(installed)) as conn, conn:
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone() == (79,)
        assert conn.execute('SELECT version FROM schema_changes ORDER BY version').fetchall() == [(c.version,) for c in regular.CHANGES]
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='entry_field_choices' AND type='table'").fetchone()
    missing = tmp_path / 'missing.db'
    with pytest.raises(SystemExit) as error:
        main(['upgrade-schema', str(missing)])
    assert error.value.code == 1
    assert not missing.exists()


def test_competing_regular_startups_install_once(installed, independent_regular_ddl):
    barrier = threading.Barrier(2)
    errors = []

    def start():
        try:
            with closing(sqlite3.connect(installed, timeout=5)) as conn, conn:
                conn.execute('PRAGMA foreign_keys=ON')
                barrier.wait()
                regular.migrate(conn)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    with closing(sqlite3.connect(installed)) as conn, conn:
        assert conn.execute('SELECT version FROM schema_changes ORDER BY version').fetchall() == [(c.version,) for c in regular.CHANGES]
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone() == (79,)


def test_completed_regular_validation_leaves_writer_available(installed, monkeypatch, independent_regular_ddl):
    with closing(sqlite3.connect(installed)) as conn, conn:
        # Match Store's production journal mode; a rollback journal naturally
        # makes every retained reader delay the writer's commit.
        conn.execute('PRAGMA journal_mode=WAL')
        regular.migrate(conn)
    entered, release = threading.Event(), threading.Event()
    validate, errors = regular.validate_version, []
    def slow(*args, **kwargs):
        result = validate(*args, **kwargs)
        entered.set()
        assert release.wait(15)
        return result
    monkeypatch.setattr(regular, 'validate_version', slow)
    def inspect():
        try:
            with closing(sqlite3.connect(installed, timeout=.1)) as conn, conn:
                regular.migrate(conn)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=inspect)
    worker.start()
    try:
        assert entered.wait(15)
        with closing(sqlite3.connect(installed, timeout=.1)) as conn, conn:
            conn.execute("UPDATE settings SET value='continuing writer' WHERE key='retained-setting'")
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive() and not errors


def test_regular_change_repairs_retired_preparation_capture_without_rewriting_history(installed, monkeypatch):
    from yt_playlist.core import genre_identity
    from yt_playlist.core.migrations import v085_genre_preferences
    from yt_playlist.core.preparation import genre_preferences
    monkeypatch.setattr(regular, 'CHANGES', regular.CHANGES[:3])
    with closing(sqlite3.connect(installed)) as conn, conn:
        conn.row_factory = sqlite3.Row
        with conn:
            genre_identity.seed_catalog(conn)
            conn.execute("INSERT INTO genre_whitelist VALUES ('Rock')")
        job = genre_preferences.CHOICES
        job.install(conn)
        while job.step(conn,batch_size=2).status != 'prepared':
            pass
        regular.migrate(conn,changes=regular.CHANGES[:2])
        # Reproduce the former explicit v85 implementation on a genuine v79
        # predecessor; its dropped whitelist left catalog capture dangling.
        with monkeypatch.context() as former:
            former.setattr(v085_genre_preferences,'retire_preparation_capture',lambda conn:None)
            migrate(conn,registry=registry.MIGRATIONS[:85])
        before = legacy_history(conn)
        evidence = list(map(tuple,conn.execute('SELECT * FROM prepared_genre_choice_originals')))
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='prepare_genre_choices_dependency_genre_catalog_insert'").fetchone()
        regular.migrate(conn)
        assert legacy_history(conn) == before
        assert list(map(tuple,conn.execute('SELECT * FROM prepared_genre_choice_originals'))) == evidence
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='prepare_genre_choices_dependency_genre_catalog_insert'").fetchone() is None
        migrate(conn,registry=registry.MIGRATIONS[:89])
        assert legacy_history(conn)[:85] == before


@pytest.fixture(scope='module')
def taste_retirement_journey(tmp_path_factory):
    from yt_playlist.core.store import Store
    from yt_playlist.core.preparation import taste_axes

    with ExitStack() as resources, pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('taste-retirement-home')))
        bound_catalog(patch)
        source = Store(tmp_path_factory.mktemp('taste-retirement') / 'installed.db')
        resources.callback(source.conn.close)
        install_numbered_input(source.conn, 79)
        with source.conn:
            source.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (3,'Listener','credential',1)")
            source.conn.execute("INSERT INTO settings VALUES ('retained-setting','original')")
            source.conn.execute("INSERT INTO rec_weights VALUES ('genre:Rock',1.25,10)")
        taste_axes.JOB.install(source.conn)
        for _ in range(20):
            if taste_axes.JOB.step(source.conn, batch_size=2).status == 'prepared':
                break
        else:
            pytest.fail('Taste preparation did not finish')
        regular.migrate(source.conn, changes=regular.CHANGES[:3])
        yield dict(conn=source.conn, source=source, version=79, resources=resources,
                   evidence=list(map(tuple, source.conn.execute('SELECT * FROM prepared_taste_originals'))))


def assert_taste_retirement_history(state):
    assert [(row[0], row[1]) for row in legacy_history(state['conn'])] == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS[:state['version']]]
    assert list(map(tuple, state['conn'].execute('SELECT * FROM prepared_taste_originals'))) == state['evidence']
    assert not state['conn'].execute('PRAGMA foreign_key_check').fetchone()


def test_taste_retirement_input_keeps_actual_prepared_originals(taste_retirement_journey):
    assert taste_retirement_journey['evidence']
    assert_taste_retirement_history(taste_retirement_journey)


@pytest.mark.parametrize('version', range(80, 88))
def test_actual_taste_predecessor_keeps_prepared_evidence(taste_retirement_journey, version):
    migrate(taste_retirement_journey['conn'], registry=registry.MIGRATIONS[:version])
    taste_retirement_journey['version'] = version
    assert_taste_retirement_history(taste_retirement_journey)


@pytest.mark.parametrize('former', [False, True])
def test_taste_capture_retirement_preserves_evidence_and_supported_upgrade(taste_retirement_journey, monkeypatch, former):
    from yt_playlist.core.migrations import v088_taste_axes
    from tests.test_installed_fixture import state

    journey = taste_retirement_journey
    monkeypatch.setattr(regular, 'CHANGES', regular.CHANGES[:4])
    conn = journey['resources'].enter_context(closing(sqlite3.connect(':memory:')))
    conn.row_factory = sqlite3.Row
    journey['conn'].backup(conn)
    with conn:
        with monkeypatch.context() as historical:
            if former:
                historical.setattr(v088_taste_axes,'retire_preparation_capture',lambda conn:None)
            migrate(conn,registry=registry.MIGRATIONS[:88])
        before=legacy_history(conn)
        if former:
            assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='prepare_taste_axes_dependency_genre_catalog_insert'").fetchone()
            with pytest.raises(sqlite3.OperationalError,match='axis|facet|rec_play_grad|rec_grad_log'):
                conn.execute('UPDATE genre_catalog SET name=name WHERE key=(SELECT min(key) FROM genre_catalog)')
            conn.rollback()
        regular.migrate(conn)
        assert legacy_history(conn)==before
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name LIKE 'prepare_taste_axes_dependency_%'").fetchone()
        with conn:
            conn.execute('UPDATE genre_catalog SET name=name WHERE key=(SELECT min(key) FROM genre_catalog)')
        assert list(map(tuple,conn.execute('SELECT * FROM prepared_taste_originals'))) == journey['evidence']
        if former:
            # Both genuine v88 routes reach the same repaired SQL and originals.
            # The common continuation therefore executes each later owner once.
            assert state(conn) == journey['repaired_state']
            journey['conn'] = journey['repaired']
            journey['version'] = 88
            journey['prefix'] = legacy_history(journey['conn'])
        else:
            journey['repaired'] = conn
            journey['repaired_state'] = state(conn)


@pytest.mark.parametrize('version', range(89, 103))
def test_actual_taste_successor_keeps_retirement_and_originals(taste_retirement_journey, version):
    migrate(taste_retirement_journey['conn'], registry=registry.MIGRATIONS[:version])
    taste_retirement_journey['version'] = version
    assert_taste_retirement_history(taste_retirement_journey)


def test_actual_taste_account_rename_keeps_originals(taste_retirement_journey):
    from tests.core.test_early_schema_upgrade import rename_owner

    rename_owner(taste_retirement_journey)
    assert_taste_retirement_history(taste_retirement_journey)


def test_actual_taste_program_restoration_keeps_originals(taste_retirement_journey):
    from tests.core.test_early_schema_upgrade import restore_programs

    restore_programs(taste_retirement_journey)
    assert_taste_retirement_history(taste_retirement_journey)


@pytest.mark.parametrize('version', range(104, len(registry.MIGRATIONS) + 1))
def test_actual_final_taste_owner_keeps_retirement_and_originals(taste_retirement_journey, version):
    migrate(taste_retirement_journey['conn'], registry=registry.MIGRATIONS[:version])
    taste_retirement_journey['version'] = version
    assert_taste_retirement_history(taste_retirement_journey)


@pytest.mark.parametrize('version', range(5, len(regular.CHANGES) + 1))
def test_actual_regular_taste_owner_keeps_retirement_and_originals(taste_retirement_journey, version):
    regular.migrate(taste_retirement_journey['conn'], changes=regular.CHANGES[:version])
    assert regular.validate(taste_retirement_journey['conn']) == version
    assert_taste_retirement_history(taste_retirement_journey)
    assert legacy_history(taste_retirement_journey['conn'])[:88] == taste_retirement_journey['prefix']
