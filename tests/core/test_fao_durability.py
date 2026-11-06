"""Failed file publication keeps SQL evidence available for a safe retry."""
from pathlib import Path

import pytest

from yt_playlist.core import fao, profile_files, sample_files
from yt_playlist.core.preparation import profiles
from tests.core.test_profile_files import seed
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
