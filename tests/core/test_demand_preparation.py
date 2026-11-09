"""Demand preparation preserves held identity, ordering, unresolved data and writes."""
from dataclasses import replace
import json

import pytest

from tests.store_fixture_support import (
    numbered_store as installed, preparation_ddl_connection, publication_preparation_input,
)
from yt_playlist.core import albums
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import recommendation_demand as preparation


VALUES = {
    'choice_window:albums': {'opportunity_id': 'retain-paid-choice', 'window': 'exact saved window'},
    'choice_window:mixes': {},
    'choice_window:radio': {'opportunity_id': 'retain-radio', 'window': ''},
    'album_menu_refresh': {'epoch': 123, 'exclude': ['release', 'unadmitted', 'release']},
    'home_genre_request': {'epoch': 125, 'include_genres': ['known-genre', 'Unknown captured', 'known-genre'],
                           'avoid_genres': [], 'held_by_vibe': False, 'suspended_by_vibe': True},
}


pytestmark = pytest.mark.usefixtures('publication_preparation_input')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Demand preparation does not construct an HTTP application."""


def seed(store):
    with store.conn:
        albums.observe(store.conn, 'release', title='Release', artist='Artist')
        store.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('known-genre','Known label','custom')")
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)',
                              [(surface, json.dumps(value, indent=2), 100) for surface, value in VALUES.items()])


def finish(conn):
    for _ in range(30):
        progress = preparation.JOB.step(conn, batch_size=1)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Demand preparation did not finish')


@pytest.mark.parametrize('installed', [79, 86], indirect=True)
def test_exact_demands_and_continuing_edits_keep_installed_owners(installed):
    store, _ = installed
    seed(store)
    raw = [tuple(row) for row in store.conn.execute('SELECT * FROM rec_proposals ORDER BY surface')]
    preparation.JOB.install(store.conn)
    first = preparation.JOB.step(store.conn, batch_size=1)
    assert first.processed == 1 and first.status == 'copying'
    finish(store.conn)
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM rec_proposals ORDER BY surface')] == raw
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM prepared_demand_originals ORDER BY surface')] == raw
    assert tuple(store.conn.execute("SELECT opportunity_key,window_text FROM prepared_listening_choice_windows WHERE format='albums'").fetchone()) == (
        'retain-paid-choice', 'exact saved window')
    assert not store.conn.execute("SELECT 1 FROM prepared_listening_choice_windows WHERE format='mixes'").fetchone()
    assert [tuple(row) for row in store.conn.execute('SELECT browse_id,album_provider_id FROM prepared_album_menu_exclusions ORDER BY position')] == [
        ('release', 'release'), ('unadmitted', None), ('release', 'release')]
    assert tuple(store.conn.execute('SELECT has_includes,has_avoids,held_by_vibe,suspended_by_vibe FROM prepared_home_genre_requests').fetchone()) == (1, 1, 0, 1)
    assert [tuple(row) for row in store.conn.execute("SELECT raw_key,genre_key,resolution_reason FROM prepared_home_genre_items WHERE kind='include' ORDER BY position")] == [
        ('known-genre', 'known-genre', None), ('Unknown captured', None, 'unknown_genre'), ('known-genre', 'known-genre', None)]
    assert not store.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='listening_choice_windows'").fetchone()
    # A source-key rename must withdraw its staged identity even when the new
    # key is unrelated and sorts before the fixed initial checkpoint.
    with store.conn:
        store.conn.execute("UPDATE rec_proposals SET surface='retained-original' WHERE surface='choice_window:albums'")
    store.rec_proposals.put_proposals('choice_window:radio', {'opportunity_id': 'new-radio', 'window': 'new'}, 200)
    finish(store.conn)
    assert not store.conn.execute("SELECT 1 FROM prepared_listening_choice_windows WHERE format='albums'").fetchone()
    assert store.conn.execute("SELECT opportunity_key FROM prepared_listening_choice_windows WHERE format='radio'").fetchone()[0] == 'new-radio'
    with store.conn:
        albums.observe(store.conn, 'unadmitted', title='New admission', artist='Artist')
        store.conn.execute("INSERT INTO genre_alias VALUES ('unknown captured','Unknown captured','known-genre','custom')")
    assert preparation.JOB.progress(store.conn).prepared_revision is None
    finish(store.conn)
    assert store.conn.execute("SELECT album_provider_id FROM prepared_album_menu_exclusions WHERE position=1").fetchone()[0] == 'unadmitted'
    assert store.conn.execute("SELECT genre_key FROM prepared_home_genre_items WHERE kind='include' AND position=1").fetchone()[0] == 'known-genre'
    assert store.rec_proposals.get_proposals('home_genre_request') == VALUES['home_genre_request']
    store.rec_proposals.put_proposals('home_genre_request', {}, 300)
    finish(store.conn)
    assert not store.conn.execute('SELECT 1 FROM prepared_home_genre_items').fetchone()
    assert tuple(store.conn.execute('SELECT epoch,has_includes,has_avoids,held_by_vibe FROM prepared_home_genre_requests').fetchone()) == (None, 0, 0, None)


@pytest.mark.parametrize('installed', [86], indirect=True)
def test_interrupted_batch_and_invalid_source_require_explicit_recovery(installed, monkeypatch, preparation_ddl_connection):
    store, _ = installed
    seed(store)
    preparation.JOB.install(store.conn)
    checkpoint = preparation.JOB.step(store.conn, batch_size=1)
    job_copy = preparation.JOB.copy
    def interrupted(*args):
        job_copy(*args)
        raise KeyboardInterrupt('controlled batch interruption')
    with monkeypatch.context() as patch:
        patch.setattr(preparation, 'JOB', replace(preparation.JOB, copy=interrupted))
        with pytest.raises(KeyboardInterrupt):
            preparation.JOB.step(store.conn, batch_size=1)
    assert preparation.JOB.progress(store.conn) == checkpoint
    with store.conn:
        store.conn.execute("UPDATE rec_proposals SET payload='{bad' WHERE surface='home_genre_request'")
    with pytest.raises(ValueError):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    regular.migrate(preparation_ddl_connection)
    store.rec_proposals.put_proposals('home_genre_request', VALUES['home_genre_request'], 200)
    with pytest.raises(MigrationError, match='retry explicitly'):
        preparation.JOB.step(store.conn)
    preparation.JOB.step(store.conn, retry=True)
    finish(store.conn)
    assert store.conn.execute('SELECT count(*) FROM prepared_home_genre_items').fetchone()[0] == 3


@pytest.mark.parametrize('installed', [86], indirect=True)
@pytest.mark.parametrize('value', [{'include_genres': ['known-genre']}, {'epoch': 1, 'held_by_vibe': 1},
                                  {'epoch': 1, 'avoid_genres': ['']}, {'epoch': 1, 'include_genres': ['x'] * 101}])
def test_invalid_demand_keeps_the_original_and_fails_explicitly(installed, value):
    store, _ = installed
    store.rec_proposals.put_proposals('home_genre_request', value, 100)
    preparation.JOB.install(store.conn)
    with pytest.raises(ValueError):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert store.rec_proposals.get_proposals('home_genre_request') == value
