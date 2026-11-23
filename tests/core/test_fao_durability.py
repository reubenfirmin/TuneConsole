"""Failed file publication keeps SQL evidence available for a safe retry."""
from pathlib import Path

import pytest

from yt_playlist.core import fao, profile_files, sample_files
from tests.core.test_profile_files import payload, seed
from tests.test_audio_vibes import sample


@pytest.mark.parametrize('domain', ['samples', 'profiles'])
def test_domain_publication_failure_keeps_sql_evidence_and_retry_can_commit(file_backed_store, monkeypatch, domain):
    store = file_backed_store
    if domain == 'samples':
        value = sample()
        root = sample_files.directory(store.conn, store._evidence_key)
        def publish():
            store.audio_vibes.save_sample(value, 1)
        def unchanged():
            row = store.conn.execute('SELECT id,file_status,filename FROM sample_attributes').fetchone()
            assert tuple(row[:2]) == (value['id'], 'pending')
            assert not Path(row['filename']).exists()
            assert store.audio_vibes.samples(value['run_id']) == []
            assert store.audio_vibes.sample_receipts(value['run_id']) == []
        def committed():
            assert store.audio_vibes.samples(value['run_id']) == [value]
    else:
        keys, bodies = seed(store)
        root = profile_files.directory(store.conn)
        raw, digest = payload('successor')
        def publish():
            store.musical_profiles.publish_recordings([(keys[0], raw, 'v2', digest)], 2)
        def unchanged():
            assert [profile_files.read(store.conn, 'recording', key) for key in keys] == [bodies[key] for key in keys]
        def committed():
            assert [profile_files.read(store.conn, 'recording', key) for key in keys] == [raw, bodies[keys[1]]]

    replace = fao.os.replace
    failed = False
    def interrupted(source, target):
        nonlocal failed
        if Path(target).is_relative_to(root):
            failed = True
            raise OSError('file publication interrupted')
        return replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(fao.os, 'replace', interrupted)
        with pytest.raises(OSError, match='file publication interrupted'):
            publish()
    assert failed and not store.conn.in_transaction
    unchanged()
    publish()
    committed()
