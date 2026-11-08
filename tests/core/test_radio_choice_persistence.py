"""Radio replacement recovery retains the selected branch and its offered menu."""
import json

import pytest

from tests.core.test_append_operation_preparation import seed
from tests.core.test_append_publication import stage
from tests.core.test_predecessor_writes import consumer_installed as installed
from yt_playlist.core import collection_recovery, radio_storage
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import append_operations, append_publication
from yt_playlist.core.store import Store
from yt_playlist.web.radio_persistence import RadioPersistence


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Checkpoint persistence does not construct an HTTP application."""


@pytest.mark.parametrize('version', [79, 93, 97, 107])
def test_empty_radio_predecessor_keeps_pending_ddl_and_real_revision_guards(
        _bounded_installed_schema, version):
    template = _bounded_installed_schema(version)
    conn = template.source.conn
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert regular.validate(conn) == 0
    assert template.revision_guards
    assert template.source._snapshot_revisions.conn is conn
    if version >= 94:
        assert not conn.execute('SELECT 1 FROM radio_sessions').fetchone()
    else:
        assert not conn.execute('SELECT 1 FROM settings WHERE key=?', (radio_storage.SESSION_KEY,)).fetchone()


def test_page_snapshot_does_not_adopt_a_newer_checkpoint_write_token(file_backed_store):
    store = file_backed_store
    _, _, _, state = seed(store, 107, append=True)
    persistence = RadioPersistence(store.radio_checkpoints)
    original_token = persistence.revision
    newer = {**state, 'error': 'Another worker owns this checkpoint'}
    with store.conn:
        next_token = radio_storage.save(store.conn, newer, expected=original_token, now=4)
    with store.read_view() as reader:
        assert persistence.view(reader) == {**newer, 'halted': False, 'stopped': False}
    assert persistence.revision == original_token
    with pytest.raises(ValueError, match='changed.*worker'):
        persistence._write(json.dumps({**state, 'error': 'Stale worker'}))
    assert radio_storage.load(store.conn) == (newer, next_token)


def test_append_receipt_retains_provider_fields_after_restart(file_backed_store):
    store = file_backed_store
    _, _, _, state = seed(store, 107, append=True)
    state['pending_result']['provider_details'] = {'attempt': 'original', 'optional': None}
    token = radio_storage.load_published(store.conn)[1]
    with store.conn:
        radio_storage.save(store.conn, state, expected=token, now=4)
    assert radio_storage.load(store.conn)[0] == state
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert radio_storage.load_published(reopened.conn)[0] == state
        assert radio_storage.load(reopened.conn)[0] == state
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        reopened.conn.close()

def publish_choice(store, version, choice):
    """Publish an actual captured checkpoint below its restart/upgrade consumer."""
    names, sources, _, state = seed(store, version)
    name, source = names[1], sources[1]
    state['replacement']['choice_id'] = choice
    if version < 94:
        original = json.dumps(state, ensure_ascii=False, indent=2)
        store.settings.set_setting(radio_storage.SESSION_KEY, original)
    else:
        token = radio_storage.load_published(store.conn)[1]
        with store.conn:
            radio_storage.save(store.conn, state, expected=token, now=4)

    if version < 98:
        owner = append_publication.OWNERS[name]
        identity = stage(owner, store.conn)
        owner.publish(store.conn, stage=identity)
        row = append_operations.head(store.conn, name, source)
        assert owner.selected(store.conn) == identity
        if version < 94:
            assert owner.value(store.conn, identity, source) == original
        assert collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'],
            kind='radio-session', owner=source) == state
    else:
        row = store.conn.execute('SELECT * FROM radio_operations').fetchone()
        assert radio_storage.load_published(store.conn)[0] == state

    return state, row


@pytest.mark.parametrize('installed', [79, 93, 97, 107], indirect=True)
@pytest.mark.parametrize(('choice', 'branch'), [
    ({'id': 'branch', 'menu_id': 'offered-menu'}, 'branch'),
    ('historical-choice', 'historical-choice'),
    (None, None),
], ids=['offered-branch', 'historical-choice', 'absent-choice'])
def test_choice_and_menu_survive_checkpoint_publication_and_restart(installed, choice, branch):
    store, version = installed
    state, row = publish_choice(store, version, choice)
    assert row['choice_id'] == branch
    assert radio_storage.load(store.conn)[0] == state
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert radio_storage.load(reopened.conn)[0] == state
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        reopened.conn.close()
