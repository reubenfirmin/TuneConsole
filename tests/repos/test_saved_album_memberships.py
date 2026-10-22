"""Album reconciliation needs successful observations of its original account cohort."""
import pytest

from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store


def album(browse):
    return dict(browse=browse,title=browse,artist='Artist')


@pytest.mark.parametrize('upgrade', [False,'retained','already-discarded'])
def test_unobserved_legacy_owner_survives_deactivation_and_restart(tmp_path,upgrade):
    path = tmp_path / 'library.db'
    store = Store(path)
    if upgrade:
        migrate(store.conn,registry=MIGRATIONS[:29])
        # Populate an installed database before the repair's cohort exists.
        with store.conn:
            store.conn.executemany('INSERT INTO identities(id,label,credential_ref,is_master,active) '
                                  'VALUES (?,?,\'bridge\',?,1)', [(1,'Main',1),(2,'Other',0)])
            store.conn.execute("INSERT INTO albums(id,title,metadata_rank) VALUES (1,'Legacy',2)")
            store.conn.execute("INSERT INTO album_provider_ids VALUES ('youtube','unknown',1)")
            store.conn.execute('INSERT INTO identity_state_archive VALUES (?,?,?,?)',
                               ('album_legacy:saved_albums','unknown','{"browse_id":"unknown","title":"Legacy"}',
                                'original album metadata or relationship'))
            if upgrade=='retained':
                store.conn.execute("INSERT INTO album_library_memberships(album_id,source) VALUES (1,'legacy_combined')")
            store.conn.execute('INSERT INTO album_library_snapshots VALUES (1,1)')
            store.conn.execute('UPDATE identities SET active=0 WHERE id=2')
        first,second = 1,2
        store.init_schema()
    else:
        store.init_schema()
        first = store.identities.upsert_identity('Main','bridge',None,True)
        second = store.identities.upsert_identity('Other','bridge','other',False)
        store.collection.add_saved_album(album('unknown'))
        store.collection.replace_saved_album_snapshots({first:[]},now=1)
        with store.conn:
            store.conn.execute('UPDATE identities SET active=0 WHERE id=?', (second,))
    store.conn.close()
    store = Store(path)
    try:
        store.init_schema()
        store.collection.replace_saved_album_snapshots({first:[]},now=2)
        assert store.collection.saved_album_ids()=={'unknown'}
        assert {r[0] for r in store.conn.execute('SELECT identity_id FROM album_library_legacy_cohort')}=={first,second}
        # An inactive account's submitted snapshot is not a successful observation.
        assert store.collection.replace_saved_album_snapshots({second:[]},now=3)==set()
        assert store.collection.saved_album_ids()=={'unknown'}
        with store.conn:
            store.conn.execute('UPDATE identities SET active=1 WHERE id=?', (second,))
        store.collection.replace_saved_album_snapshots({second:[]},now=4)
        assert store.collection.saved_album_ids()==set()
    finally:
        store.conn.close()


def test_new_account_does_not_expand_the_original_legacy_cohort(store):
    owner = store.identities.upsert_identity('Main','bridge',None,True)
    store.collection.add_saved_album(album('unknown'))
    store.identities.upsert_identity('New','bridge','new',False)
    store.collection.replace_saved_album_snapshots({owner:[]})
    assert store.collection.saved_album_ids()==set()


def test_legacy_without_any_original_accounts_cannot_be_resolved_vacuously(store):
    store.collection.add_saved_album(album('unknown'))
    owner = store.identities.upsert_identity('New','bridge',None,True)
    store.collection.replace_saved_album_snapshots({owner:[]})
    assert store.collection.saved_album_ids()=={'unknown'}


def test_new_legacy_batch_needs_new_observations_and_retains_scoped_ownership(store):
    first = store.identities.upsert_identity('Main','bridge',None,True)
    second = store.identities.upsert_identity('Other','bridge','other',False)
    store.collection.add_saved_album(album('old'))
    store.collection.replace_saved_album_snapshots({first:[album('owned')],second:[]})
    assert store.collection.saved_album_ids()=={'owned'}
    store.collection.replace_saved_albums([album('new')])
    store.collection.replace_saved_album_snapshots({first:[album('owned')]})
    assert store.collection.saved_album_ids()=={'owned','new'}
    store.collection.replace_saved_album_snapshots({second:[]})
    assert store.collection.saved_album_ids()=={'owned'}


def test_added_legacy_evidence_invalidates_older_fetches_and_observations(store):
    first = store.identities.upsert_identity('Main','bridge',None,True)
    second = store.identities.upsert_identity('Other','bridge','other',False)
    store.collection.add_saved_album(album('old'))
    store.collection.replace_saved_album_snapshots({first:[]})
    revision = store.collection.saved_album_revision(first)
    store.collection.add_saved_album(album('new'))
    assert store.collection.replace_saved_album_snapshots({first:[]},expected_revisions={first:revision})==set()
    store.collection.replace_saved_album_snapshots({second:[]})
    assert store.collection.saved_album_ids()=={'old','new'}
    store.collection.replace_saved_album_snapshots({first:[]})
    assert store.collection.saved_album_ids()==set()


def test_stale_snapshot_is_rejected_across_store_connections(tmp_path):
    path = tmp_path / 'library.db'
    first,second = Store(path),Store(path)
    try:
        first.init_schema()
        owner = first.identities.upsert_identity('Main','bridge',None,True)
        second.init_schema()
        revision = first.collection.saved_album_revision(owner)
        second.collection.add_saved_album(album('saved'),identity_id=owner)
        assert first.collection.replace_saved_album_snapshots({owner:[]},expected_revisions={owner:revision})==set()
        assert first.collection.saved_album_ids()=={'saved'}
        assert not first.conn.execute('SELECT * FROM album_library_snapshots').fetchall()
    finally:
        first.conn.close()
        second.conn.close()


def test_snapshot_and_revision_rollback_together(store):
    owner = store.identities.upsert_identity('Main','bridge',None,True)
    store.collection.add_saved_album(album('saved'),identity_id=owner)
    revision = store.collection.saved_album_revision(owner)
    with pytest.raises(KeyError):
        store.collection.replace_saved_album_snapshots({owner:[{'title':'Invalid'}]},expected_revisions={owner:revision})
    assert store.collection.saved_album_revision(owner)==revision
    assert store.collection.saved_album_ids()=={'saved'}
    assert not store.conn.execute('SELECT * FROM album_library_snapshots').fetchall()
    assert store.collection.replace_saved_album_snapshots({owner:[]},expected_revisions={owner:revision})=={owner}
    # Even an empty complete snapshot invalidates another in-flight snapshot.
    assert store.collection.replace_saved_album_snapshots({owner:[album('stale')]},expected_revisions={owner:revision})==set()
