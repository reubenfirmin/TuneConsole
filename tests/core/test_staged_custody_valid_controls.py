"""A restored rejection input must publish before any corruption is applied."""
import json
from pathlib import Path

import pytest

from tests.core.test_predecessor_writes import consumer_installed, installed


@pytest.mark.parametrize('consumer_installed', [93], indirect=True)
def test_bounded_custody_keeps_exact_installed_history_and_publishes_valid_report(
        consumer_installed, staged_custody):
    from tests.core import test_import_publication as cases

    store, _ = consumer_installed
    history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    raw, revision = staged_custody(consumer_installed, 'spotify-import', cases.rejection_inputs)
    assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('private-after-custody-restore') is None
        store.settings.set_setting('private-after-custody-restore', 'changed')
        assert not snapshot.publish(lambda: pytest.fail('Published changed restored input'))
    owner = cases.publication.OWNER
    assert owner.selected(store.conn) is None
    owner.publish(store.conn, revision=revision)
    assert owner.value(store.conn, revision) == raw
    assert cases.spotify_import.load_job(store) == cases.entity_names.account_document(json.loads(raw))


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('family', ['actions', 'import', 'tv'])
def test_restored_rejection_input_can_publish_valid_original(
        installed, staged_custody, monkeypatch, family):
    store, _ = installed
    if family == 'actions':
        from tests.core import test_action_publication as cases

        logical = tuple((cases.preparation.table(suffix), 'backup_filename')
                        for suffix in ('backups', 'steps'))
        identities, backup, source_inode = staged_custody(
            installed, 'action-ledger', cases.rejection_inputs,
            logical_coordinates=logical)
        monkeypatch.chdir(store._db_path.parent)
        witness = Path('retained-backup.json')
        witness.write_bytes(backup)
        assert (witness.stat().st_dev, witness.stat().st_ino) != source_inode
        originals = {row['id']: dict(row)
                     for row in store.conn.execute('SELECT * FROM main.actions')}
        owner = cases.publication.OWNER
        assert owner.selected(store.conn) is None
        stage = owner.step(store.conn)
        assert stage.complete
        owner.publish(store.conn, stage=stage.id)
        assert owner.selected(store.conn) == stage.id
        assert {identity: owner.value(store.conn, stage.id, str(identity))
                for identity in identities} == originals
        assert witness.read_bytes() == backup
    elif family == 'import':
        from tests.core import test_import_publication as cases

        raw, revision = staged_custody(installed, 'spotify-import', cases.rejection_inputs)
        owner = cases.publication.OWNER
        assert owner.selected(store.conn) is None
        owner.publish(store.conn, revision=revision)
        assert owner.selected(store.conn) == revision
        assert owner.value(store.conn, revision) == raw
        assert cases.spotify_import.load_job(store) == cases.entity_names.account_document(json.loads(raw))
    else:
        from tests.core import test_tv_publication as cases

        surface = 'tv_pool'
        originals, revision = staged_custody(
            installed, ('tv', surface, 'aliases'),
            lambda source: cases.rejection_inputs(source, source.conn, surface=surface, with_aliases=True))
        owner = cases.publication.SOURCES[surface]
        assert owner.selected(store.conn) is None
        owner.publish(store.conn, revision=revision)
        assert owner.selected(store.conn) == revision
        assert store.rec_proposals._proposal_json(surface) == originals[surface]
        assert store.rec_proposals.get_proposals(surface) == json.loads(originals[surface])
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
