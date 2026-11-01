"""Radio cutover retains exact custody and fences files, sources and queue rows."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_legacy_radio_upgrade import installed_radio
from tests.core.test_radio_preparation import finish, header
from yt_playlist.core import catalog, radio_storage
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import radio_publication as publication, radio_session as preparation
from yt_playlist.core.store import Store
from yt_playlist.web.radio_persistence import RadioPersistence


def prepare(conn):
    preparation.JOB.install(conn)
    return finish(conn).prepared_revision


def test_preopened_readers_and_compatible_writer_keep_exact_checkpoint(tmp_path, monkeypatch):
    store, state, raw = installed_radio(tmp_path / 'radio.db')
    other = Store(store._db_path)
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        persistence = RadioPersistence(other.settings)
        assert persistence.load()['episode_id'] == state['episode_id']
        history = list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        revision = prepare(store.conn)
        publication.publish(store.conn,revision=revision)
        publication.publish(store.conn,revision=revision)
        assert publication.selected(other.conn) == revision
        real_read = preparation.read
        reads = []
        def reading(row):
            reads.append(row['generation'])
            return real_read(row)
        monkeypatch.setattr(preparation,'read',reading)
        assert other.settings.get_setting(preparation.KEY) == raw
        assert radio_storage.load_published(other.conn) == (state,None)
        assert radio_storage.load(other.conn) == (state,None)
        assert persistence.load()['tracks'] == state['tracks']
        assert reads == [revision] * 4
        assert not radio_storage.available(other.conn)
        assert dict(store.conn.execute('SELECT key,value FROM main.settings'))[preparation.KEY] == raw
        assert list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        regular.validate(other.conn)
        updated = {**state,'phase':'stopped','position':2,'tuning':{'paid_result':['exact']}}
        encoded = json.dumps(updated,indent=2)
        persistence._write(encoded)
        assert publication.selected(store.conn) is None
        assert radio_storage.load_published(store.conn) == (updated,None)
        assert real_read(store.conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?',(revision,)).fetchone()) == raw
        publication.publish(store.conn,revision=prepare(store.conn))
        assert other.settings.get_setting(preparation.KEY) == encoded
    finally:
        other.conn.close()
        store.conn.close()
    reopened = Store(tmp_path / 'radio.db')
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.settings.get_setting(preparation.KEY) == encoded
        assert radio_storage.load_published(reopened.conn) == (updated,None)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('failure',['header','queue','corrupt-file','missing-file'])
def test_invalid_preparation_does_not_replace_installed_checkpoint(tmp_path,failure):
    store, state, raw = installed_radio(tmp_path / 'invalid.db')
    try:
        revision = prepare(store.conn)
        row = header(store.conn)
        if failure == 'header':
            with store.conn:
                store.conn.execute('UPDATE '+preparation.HEAD+" SET phase='stopped' WHERE generation=?",(revision,))
        elif failure == 'queue':
            with store.conn:
                store.conn.execute('UPDATE '+preparation.QUEUE+" SET state='queued' WHERE generation=? AND position=0",(revision,))
        elif failure == 'corrupt-file':
            Path(row['filename']).write_bytes(b'corrupt checkpoint')
        else:
            Path(row['filename']).unlink()
        with pytest.raises((MigrationError,FileNotFoundError)):
            publication.publish(store.conn,revision=revision)
        assert publication.selected(store.conn) is None
        assert radio_storage.load_published(store.conn) == (state,None)
        assert store.conn.execute('SELECT value FROM main.settings WHERE key=?',(preparation.KEY,)).fetchone()[0] == raw
        regular.migrate(store.conn)
    finally:
        store.conn.close()


@pytest.mark.parametrize('when,mutation',[('file','source'),('file','queue'),('file','header'),
                                         ('verified','source'),('verified','queue')])
def test_changes_during_verification_cannot_select_a_stale_generation(tmp_path,monkeypatch,when,mutation):
    store, state, _ = installed_radio(tmp_path / 'racing.db')
    second = sqlite3.connect(store._db_path)
    try:
        revision = prepare(store.conn)
        def change():
            with second:
                if mutation == 'source':
                    second.execute('UPDATE main.settings SET value=? WHERE key=?',
                        (json.dumps({**state,'phase':'stopped'}),preparation.KEY))
                elif mutation == 'header':
                    second.execute('UPDATE '+preparation.HEAD+" SET phase='stopped' WHERE generation=?",(revision,))
                else:
                    second.execute('UPDATE '+preparation.QUEUE+" SET state='queued' WHERE generation=? AND position=0",(revision,))
        if when == 'file':
            real_read = preparation.read
            def reading(row):
                assert not store.conn.in_transaction
                change()
                return real_read(row)
            monkeypatch.setattr(preparation,'read',reading)
        else:
            real_verify = publication.verify
            def verified(conn,revision):
                proof = real_verify(conn,revision)
                assert not conn.in_transaction
                change()
                return proof
            monkeypatch.setattr(publication,'verify',verified)
        with pytest.raises(MigrationError,match='changed (during|after) verification'):
            publication.publish(store.conn,revision=revision)
        assert publication.selected(store.conn) is None
        assert not store.conn.in_transaction
        assert not store.conn.execute('SELECT 1 FROM '+publication.GENERATIONS).fetchone()
    finally:
        second.close()
        store.conn.close()


def test_selected_custody_is_immutable_and_missing_file_has_explicit_recovery(tmp_path):
    store, state, raw = installed_radio(tmp_path / 'recovery.db')
    try:
        revision = prepare(store.conn)
        publication.publish(store.conn,revision=revision)
        old = header(store.conn)
        for table in (preparation.HEAD,preparation.QUEUE,publication.GENERATIONS):
            with pytest.raises(sqlite3.IntegrityError,match='(retained|future cleanup)'):
                with store.conn:
                    store.conn.execute('DELETE FROM '+table)
        Path(old['filename']).unlink()
        with pytest.raises(FileNotFoundError):
            radio_storage.load_published(store.conn)
        preparation.JOB.step(store.conn,retry=True)
        assert radio_storage.load_published(store.conn) == (state,None)
        assert preparation.read(old) == raw
        assert store.conn.execute('SELECT count(*) FROM '+preparation.QUEUE).fetchone()[0] == 5
        with store.conn:
            catalog.upsert(store.conn,'later','Later','Artist',preferred_key='later|artist')
        assert publication.selected(store.conn) is None
        assert radio_storage.load_published(store.conn) == (state,None)
        publication.publish(store.conn,revision=prepare(store.conn))
        assert publication.selected(store.conn) > revision
        assert preparation.read(old) == raw
    finally:
        store.conn.close()


def test_interrupted_publication_retains_previous_generation_and_retry_is_atomic(tmp_path):
    store, state, raw = installed_radio(tmp_path / 'interrupted.db')
    try:
        first = prepare(store.conn)
        publication.publish(store.conn,revision=first)
        old = header(store.conn)
        with store.conn:
            store.conn.execute('UPDATE main.settings SET value=? WHERE key=?',
                (json.dumps({**state,'phase':'stopped'}),preparation.KEY))
        revision = prepare(store.conn)
        def interrupted():
            raise KeyboardInterrupt('controlled publication interruption')
        with pytest.raises(KeyboardInterrupt):
            publication.publish(store.conn,revision=revision,clock=interrupted)
        assert not store.conn.in_transaction
        assert publication.selected(store.conn) is None
        store.conn.execute('CREATE TEMP TRIGGER interrupt_radio_cutover BEFORE INSERT ON main.'+publication.HEAD+
            " BEGIN SELECT RAISE(ABORT,'controlled cutover failure'); END")
        with pytest.raises(sqlite3.IntegrityError,match='controlled cutover'):
            publication.publish(store.conn,revision=revision)
        assert list(map(tuple,store.conn.execute('SELECT revision FROM '+publication.GENERATIONS))) == [(first,)]
        assert publication.selected(store.conn) is None
        assert preparation.read(old) == raw
        regular.migrate(store.conn)
        store.conn.execute('DROP TRIGGER temp.interrupt_radio_cutover')
        publication.publish(store.conn,revision=revision)
        assert publication.selected(store.conn) == revision
    finally:
        store.conn.close()


@pytest.mark.parametrize('present,raw',[(False,None),(True,None),(True,'null'),(True,'')])
def test_absent_null_and_blank_sources_keep_original_setting_semantics(tmp_path,present,raw):
    store, _, _ = installed_radio(tmp_path / 'empty.db')
    try:
        store.init_schema(defer_metadata=True)
        with store.conn:
            store.conn.execute('DELETE FROM main.settings WHERE key=?',(preparation.KEY,))
            if present:
                store.conn.execute('INSERT INTO main.settings VALUES (?,?)',(preparation.KEY,raw))
        publication.publish(store.conn,revision=prepare(store.conn))
        assert store.settings.get_setting(preparation.KEY,'missing') == (raw if present else 'missing')
        assert radio_storage.load_published(store.conn) == (None,None)
        assert header(store.conn)['active'] == 0
    finally:
        store.conn.close()


def test_unresolved_subjects_and_frozen_native_upgrade_retain_custody(tmp_path):
    store, state, _ = installed_radio(tmp_path / 'native.db')
    try:
        state.pop('episode_id')
        state['tracks'].append({'video_id':'unknown','title':'Do not invent identity'})
        raw = json.dumps(state,indent=2)
        with store.conn:
            store.conn.execute('UPDATE main.settings SET value=? WHERE key=?',(raw,preparation.KEY))
        revision = prepare(store.conn)
        publication.publish(store.conn,revision=revision)
        retained = header(store.conn)
        assert retained['episode_resolution'] == 'missing_episode_id'
        assert radio_storage.load_published(store.conn) == (state,None)
        assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='unknown'").fetchone()
        runner.migrate(store.conn,registry=registry.MIGRATIONS)
        regular.validate(store.conn)
        assert publication.selected(store.conn) is None
        assert radio_storage.available(store.conn)
        native, token = radio_storage.load_published(store.conn)
        assert native['tracks'] == state['tracks']
        assert token[0].startswith('legacy:') and token[1] == 1
        assert preparation.read(retained) == raw
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()
