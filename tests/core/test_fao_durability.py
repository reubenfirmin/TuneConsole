"""Publication cannot hand SQL an unsynced pathname, including after retry."""
from contextlib import closing
from pathlib import Path

import pytest

from yt_playlist.core import fao, profile_files, sample_files
from yt_playlist.core.preparation import profiles
from tests.core.test_profile_files import seed
from tests.test_audio_vibes import sample


@pytest.mark.parametrize('operation', ['ordinary', 'staged', 'copy', 'restore'])
@pytest.mark.parametrize('precreated', [False, True])
def test_publication_syncs_all_ancestors_before_rename_and_retries(tmp_path, monkeypatch, operation, precreated):
    source = tmp_path / 'source' / 'original.json'
    fao.write_bytes(source, b'original bytes')
    manifest = fao.export(source.parent, [source], tmp_path / 'backup')
    target = tmp_path / 'new-root' / 'owner' / 'shard' / 'original.json'
    if precreated:
        target.parent.mkdir(parents=True)

    def publish():
        if operation == 'ordinary':
            fao.write_bytes(target, b'original bytes', immutable=True)
        elif operation == 'staged':
            with closing(fao.stage_bytes(target, b'original bytes')) as stage:
                stage.publish()
        elif operation == 'copy':
            fao.copy(source, target)
        else:
            fao.restore(target.parent, manifest, expected=[target])

    sync, replace = fao._sync, fao.os.replace
    events, fail = [], True
    ancestors = [target.parent.resolve(), *target.parent.resolve().parents]

    def synced(directory):
        events.append(('sync', Path(directory)))
        if fail and Path(directory) == target.parent.parent:
            raise OSError('ancestor sync interrupted')
        sync(directory)

    def renamed(old, new):
        if Path(new) == target:
            # Every ancestor must be durable before the new file is visible.
            assert all(('sync', ancestor) in events for ancestor in ancestors)
            events.append(('rename', target))
        return replace(old, new)

    monkeypatch.setattr(fao, '_sync', synced)
    monkeypatch.setattr(fao.os, 'replace', renamed)
    with pytest.raises(OSError, match='ancestor sync interrupted'):
        publish()
    assert target.parent.exists() and not target.exists()
    fail = False
    events.clear()
    publish()
    assert fao.read_bytes(target) == b'original bytes'
    rename = events.index(('rename', target))
    assert [event for event in events[:rename] if event[1] in ancestors][-len(ancestors):] == [
        ('sync', ancestor) for ancestor in ancestors]
    assert events[rename + 1] == ('sync', target.parent)


def test_immutable_retry_syncs_existing_file_after_failed_final_directory_sync(tmp_path, monkeypatch):
    target = tmp_path / 'owner' / 'shard' / 'same.json'
    sync, calls = fao._sync, 0

    def interrupted(directory):
        nonlocal calls
        if Path(directory) == target.parent:
            calls += 1
            if calls == 2:
                raise OSError('final directory sync interrupted')
        sync(directory)

    monkeypatch.setattr(fao, '_sync', interrupted)
    with pytest.raises(OSError, match='final directory sync interrupted'):
        fao.write_bytes(target, b'exact', immutable=True)
    assert target.exists()
    original = target.stat().st_ino
    fao.write_bytes(target, b'exact', immutable=True)
    assert calls == 3 and target.stat().st_ino == original
    assert fao.read_bytes(target) == b'exact'


@pytest.mark.parametrize('domain', ['samples', 'profiles'])
def test_domain_sync_failure_keeps_sql_evidence_and_retry_can_commit(file_backed_store, monkeypatch, domain):
    store = file_backed_store
    if domain == 'samples':
        value = sample()
        root = sample_files.directory(store.conn, store._evidence_key)
        def publish():
            store.audio_vibes.save_sample(value, 1)
        def unchanged():
            assert not store.conn.execute('SELECT 1 FROM sample_attributes').fetchone()
        def committed():
            assert store.audio_vibes.samples(value['run_id']) == [value]
    else:
        keys, bodies = seed(store)
        root = profile_files.directory(store.conn)
        def publish():
            profiles.prepare(store)
        def unchanged():
            assert dict(store.conn.execute('SELECT identity_key,payload FROM rec_musical_profiles')) == bodies
            assert not store.conn.execute('SELECT 1 FROM recording_profile_files').fetchone()
        def committed():
            assert [profile_files.read(store.conn, 'recording', key) for key in keys] == [bodies[key] for key in keys]
            assert all(not row[0] for row in store.conn.execute('SELECT payload FROM rec_musical_profiles'))

    sync = fao._sync
    failed = False
    def interrupted(directory):
        nonlocal failed
        if Path(directory) == root.parent:
            failed = True
            raise OSError('domain ancestor interrupted')
        sync(directory)
    with monkeypatch.context() as patch:
        patch.setattr(fao, '_sync', interrupted)
        with pytest.raises(OSError, match='domain ancestor interrupted'):
            publish()
    assert failed and not store.conn.in_transaction
    unchanged()
    publish()
    committed()
