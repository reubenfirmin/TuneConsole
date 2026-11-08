"""Album reconciliation needs successful observations of its original account cohort."""
import pytest

from yt_playlist.core.store import Store


def album(browse):
    return dict(browse=browse,title=browse,artist='Artist')


def test_unobserved_legacy_owner_survives_deactivation_and_restart(tmp_path):
    path = tmp_path / 'library.db'
    store = Store(path)
    store.init_schema()
    first = store.accounts.upsert_account('Main','bridge',None,True)
    second = store.accounts.upsert_account('Other','bridge','other',False)
    store.collection.add_saved_album(album('unknown'))
    store.collection.replace_saved_album_snapshots({first:[]},now=1)
    with store.conn:
        store.conn.execute('UPDATE accounts SET active=0 WHERE id=?', (second,))
    store.conn.close()
    store = Store(path)
    try:
        store.init_schema()
        store.collection.replace_saved_album_snapshots({first:[]},now=2)
        assert store.collection.saved_album_ids()=={'unknown'}
        assert {r[0] for r in store.conn.execute('SELECT account_id FROM account_album_state WHERE legacy_cohort_present=1')}=={first,second}
        # An inactive account's submitted snapshot is not a successful observation.
        assert store.collection.replace_saved_album_snapshots({second:[]},now=3)==set()
        assert store.collection.saved_album_ids()=={'unknown'}
        with store.conn:
            store.conn.execute('UPDATE accounts SET active=1 WHERE id=?', (second,))
        store.collection.replace_saved_album_snapshots({second:[]},now=4)
        assert store.collection.saved_album_ids()==set()
    finally:
        store.conn.close()


def test_new_account_does_not_expand_the_original_legacy_cohort(store):
    owner = store.accounts.upsert_account('Main','bridge',None,True)
    store.collection.add_saved_album(album('unknown'))
    store.accounts.upsert_account('New','bridge','new',False)
    store.collection.replace_saved_album_snapshots({owner:[]})
    assert store.collection.saved_album_ids()==set()


def test_legacy_without_any_original_accounts_cannot_be_resolved_vacuously(store):
    store.collection.add_saved_album(album('unknown'))
    owner = store.accounts.upsert_account('New','bridge',None,True)
    store.collection.replace_saved_album_snapshots({owner:[]})
    assert store.collection.saved_album_ids()=={'unknown'}


def test_new_legacy_batch_needs_new_observations_and_retains_scoped_ownership(store):
    first = store.accounts.upsert_account('Main','bridge',None,True)
    second = store.accounts.upsert_account('Other','bridge','other',False)
    store.collection.add_saved_album(album('old'))
    store.collection.replace_saved_album_snapshots({first:[album('owned')],second:[]})
    assert store.collection.saved_album_ids()=={'owned'}
    store.collection.replace_saved_albums([album('new')])
    store.collection.replace_saved_album_snapshots({first:[album('owned')]})
    assert store.collection.saved_album_ids()=={'owned','new'}
    store.collection.replace_saved_album_snapshots({second:[]})
    assert store.collection.saved_album_ids()=={'owned'}


def test_added_legacy_evidence_invalidates_older_fetches_and_observations(store):
    first = store.accounts.upsert_account('Main','bridge',None,True)
    second = store.accounts.upsert_account('Other','bridge','other',False)
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
        owner = first.accounts.upsert_account('Main','bridge',None,True)
        second.init_schema()
        revision = first.collection.saved_album_revision(owner)
        second.collection.add_saved_album(album('saved'),account_id=owner)
        assert first.collection.replace_saved_album_snapshots({owner:[]},expected_revisions={owner:revision})==set()
        assert first.collection.saved_album_ids()=={'saved'}
        assert not first.conn.execute('SELECT * FROM account_album_state WHERE snapshot_observed_at IS NOT NULL').fetchall()
    finally:
        first.conn.close()
        second.conn.close()


def test_snapshot_and_revision_rollback_together(store):
    owner = store.accounts.upsert_account('Main','bridge',None,True)
    store.collection.add_saved_album(album('saved'),account_id=owner)
    revision = store.collection.saved_album_revision(owner)
    with pytest.raises(KeyError):
        store.collection.replace_saved_album_snapshots({owner:[{'title':'Invalid'}]},expected_revisions={owner:revision})
    assert store.collection.saved_album_revision(owner)==revision
    assert store.collection.saved_album_ids()=={'saved'}
    assert not store.conn.execute('SELECT * FROM account_album_state WHERE snapshot_observed_at IS NOT NULL').fetchall()
    assert store.collection.replace_saved_album_snapshots({owner:[]},expected_revisions={owner:revision})=={owner}
    # Even an empty complete snapshot invalidates another in-flight snapshot.
    assert store.collection.replace_saved_album_snapshots({owner:[album('stale')]},expected_revisions={owner:revision})==set()
