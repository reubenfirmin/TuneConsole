"""Published playback context preserves the installed reader and writer protocol."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from tests.core.test_playback_context_preparation import finish, head, seed
from yt_playlist.core import album_playback_context, entity_names, playback_bindings
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import playback_context as preparation, playback_publication as publication
from yt_playlist.core.store import Store


def prepare(conn, owner):
    owner.worker.install(conn)
    return finish(owner.worker, conn, size=128).prepared_revision


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_preopened_readers_find_exact_attribution_and_pending_queue(installed):
    store, _ = installed
    original = seed(store)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        for owner in publication.OWNERS.values():
            owner.publish(store.conn, revision=prepare(store.conn, owner))
            assert owner.selected(other.conn) == 0
        assert {key:other.settings.get_setting(key) for key in original} == original
        assert album_playback_context.load(other.conn) == entity_names.account_document(json.loads(original[preparation.PENDING_KEY]))
        assert store.rec_ledger.choice_playback(1, 'LM', 'video', 13, 100)['opportunity_id'] == 'later-menu'
        assert not playback_bindings.available(other.conn)
        assert not album_playback_context.available(other.conn)
        regular.validate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert {key:reopened.settings.get_setting(key) for key in original} == original
        assert album_playback_context.load(reopened.conn)['videos'] == ['video', 'video', 'unadmitted']
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('state', ['absent', 'cleared', 'missing-account', 'null-account', 'no-videos', 'empty-videos'])
def test_pending_absence_account_kinds_and_video_presence_remain_distinct(installed, state):
    store, _ = installed
    originals = seed(store)
    source = preparation.PENDING_KEY
    value = json.loads(originals[source])
    if state == 'absent':
        store.settings.delete_setting(source)
        value = None
    else:
        if state == 'cleared':
            value = None
        elif state == 'missing-account':
            del value['identity_id']
        elif state == 'null-account':
            value['identity_id'] = None
        elif state == 'no-videos':
            del value['videos']
        else:
            value['videos'] = []
        store.settings.set_setting(source, json.dumps(value))
    owner = publication.SOURCES[source]
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert album_playback_context.load(store.conn) == entity_names.account_document(value)
    assert store.settings.get_setting(source, 'absent') == ('absent' if state == 'absent' else json.dumps(value))
    if state not in ('absent', 'cleared'):
        row = store.conn.execute('SELECT account_kind,has_videos FROM '+preparation.INTENTS).fetchone()
        assert row['account_kind'] == {'missing-account':'missing', 'null-account':'null'}.get(state, 'recorded')
        assert row['has_videos'] == (state != 'no-videos')


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['binding', 'intent', 'queue', 'extra-queue', 'header', 'file'])
def test_incorrect_context_or_original_never_selects_a_runtime_owner(installed, failure):
    store, _ = installed
    original = seed(store)
    owner = publication.OWNERS['bindings' if failure == 'binding' else 'pending-album']
    revision = prepare(store.conn, owner)
    with store.conn:
        if failure == 'binding':
            store.conn.execute('UPDATE '+preparation.BINDINGS+" SET opportunity_id='changed' WHERE position=0")
        elif failure == 'intent':
            store.conn.execute('UPDATE '+preparation.INTENTS+" SET account_kind='missing',raw_account_id=NULL,account_id=NULL,unresolved_account_id=NULL")
        elif failure == 'queue':
            store.conn.execute('UPDATE '+preparation.ITEMS+" SET video_id='different' WHERE position=2")
        elif failure == 'extra-queue':
            store.conn.execute('INSERT INTO '+preparation.ITEMS+' SELECT source,generation,3,video_id,entry_id,recording_id,unresolved_video_reason '
                               'FROM '+preparation.ITEMS+' WHERE position=2')
        elif failure == 'header':
            store.conn.execute('UPDATE '+preparation.tables(owner.source)[0]+" SET document_kind='cleared'")
        else:
            Path(store.conn.execute('SELECT filename FROM '+preparation.tables(owner.source)[1]+' LIMIT 1').fetchone()[0]).write_bytes(b'corrupt')
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) is None
    assert store.settings.get_setting(owner.source) == original[owner.source]
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_compatible_bindings_and_pending_writes_reconcile_without_admitting_unknowns(installed):
    store, _ = installed
    original = seed(store)
    counts = {table:store.conn.execute('SELECT count(*) FROM main.'+table).fetchone()[0]
              for table in ('identities', 'playlist_scopes', 'catalog_entries', 'album_provider_ids')}
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    pending = publication.OWNERS['pending-album']
    bindings = publication.OWNERS['bindings']
    retained = head(store.conn, pending.source)
    with store.read_snapshot() as snapshot:
        assert album_playback_context.load(snapshot.store.conn)['videos'] == ['video', 'video', 'unadmitted']
        with store.conn:
            album_playback_context.put(store.conn, None)
        assert not snapshot.publish(lambda:None)
    assert pending.selected(store.conn) is None
    assert bindings.selected(store.conn) is not None
    assert album_playback_context.load(store.conn) is None
    store.rec_ledger.bind_choice_playback(1, 'LM', 'video', 'new-choice', 9, 15, 100)
    assert bindings.selected(store.conn) is None
    assert store.rec_ledger.choice_playback(1, 'LM', 'video', 16, 100)['opportunity_id'] == 'new-choice'
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert counts == {table:store.conn.execute('SELECT count(*) FROM main.'+table).fetchone()[0] for table in counts}
    assert preparation.read_original(store.conn, retained) == original[pending.source]


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('published', [False, True])
def test_binding_writer_reads_committed_settings_and_keeps_external_fields(installed, monkeypatch, published):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['bindings']
    if published:
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    real_get = store.settings.get_setting
    def reading(key, default=None):
        assert not store.conn.in_transaction
        return real_get(key, default)
    monkeypatch.setattr(store.settings, 'get_setting', reading)
    store.rec_ledger.bind_choice_playback(1, 'LM', 'video', 'new-choice', 9, 15, 100)
    monkeypatch.undo()
    rows = json.loads(store.settings.get_setting(owner.source))
    assert all('identity_id' in row and 'account_id' not in row for row in rows)
    assert owner.selected(store.conn) is None
    assert store.rec_ledger.choice_playback(1, 'LM', 'video', 16, 100)['opportunity_id'] == 'new-choice'


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_repeated_queue_reference_invalidation_missing_files_and_retry(installed):
    store, _ = installed
    original = seed(store)
    owner = publication.OWNERS['pending-album']
    pending = json.loads(original[owner.source])
    pending['videos'] = ['video']*2048+['unadmitted']
    store.settings.set_setting(owner.source, json.dumps(pending))
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert album_playback_context.load(store.conn)['videos'] == pending['videos']
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM '+preparation.ITEMS)
    store.tracks.upsert_track('unadmitted', 'Now known', 'Artist', None, 100)
    assert owner.selected(store.conn) is None
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    current = head(store.conn, owner.source)
    path = Path(store.conn.execute('SELECT filename FROM '+preparation.tables(owner.source)[1]+
        ' WHERE generation=? LIMIT 1', (current['generation'],)).fetchone()[0])
    path.unlink()
    with pytest.raises(FileNotFoundError):
        album_playback_context.load(store.conn)
    owner.worker.step(store.conn, retry=True)
    assert album_playback_context.load(store.conn)['videos'] == pending['videos']


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', tuple(publication.OWNERS))
def test_original_large_integer_times_survive_native_real_affinity(installed, name):
    store, _ = installed
    original = seed(store)
    owner = publication.OWNERS[name]
    value = json.loads(original[owner.source])
    if isinstance(value, list):
        value[0]['at'] = 9007199254740993
    else:
        value['at'] = 9007199254740993
    raw = json.dumps(value)
    store.settings.set_setting(owner.source, raw)
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert store.settings.get_setting(owner.source) == raw
