"""Catalog rekeys preserve exact draft decisions and fence earlier workers."""
from yt_playlist.core import catalog, road_trip_draft_documents, road_trip_draft_storage


def test_rekey_draft_changes_only_declared_keys_and_fences_stale_completion(store):
    store.accounts.upsert_account('Account', 'account.json', None, True)
    recipe = store.road_trip.save_road_trip_recipe(None, 'Trip', 50, [], [], 60, 1)
    first = store.tracks.upsert_track('one', 'One', 'Artist', None, 100)
    second = store.tracks.upsert_track('two', 'Two', 'Artist', None, 100)
    old = store.tracks.catalog_subject(first)['identity_key']
    target = store.tracks.catalog_subject(second)
    state = {'revision': 'worker-input', 'keys': [old, old], 'label': old,
             'pool': [{'key': old, 'identity_key': old, 'video_id': 'one', 'label': old}],
             'picks': ['two', 'one', 'two'], 'banned': ['one'], 'building': True}
    with store.conn:
        road_trip_draft_storage.save(store.conn, recipe, state, 10)
    before = dict(store.conn.execute('SELECT * FROM road_trip_drafts').fetchone())
    selections = [tuple(row) for row in store.conn.execute(
        'SELECT * FROM road_trip_draft_entries ORDER BY selection_state,position')]
    with store.conn:
        catalog.add_alias(store.conn, old, target['recording_id'], 'test')
    revised = road_trip_draft_storage.get(store.conn, recipe)
    current_key = store.tracks.catalog_subject(first)['identity_key']
    assert current_key != old
    assert revised['keys'] == [current_key, current_key]
    assert revised['pool'][0]['key'] == revised['pool'][0]['identity_key'] == current_key
    assert revised['pool'][0]['video_id'] == 'one'
    assert revised['label'] == revised['pool'][0]['label'] == old
    assert revised['picks'] == state['picks'] and revised['banned'] == state['banned']
    assert revised['revision'] != state['revision']
    after = dict(store.conn.execute('SELECT * FROM road_trip_drafts').fetchone())
    for field in ('id', 'recipe_id', 'account_id', 'state', 'updated_at'):
        assert after[field] == before[field]
    # Account reconciliation may update recording references; it cannot reorder
    # or substitute the exact upload decisions.
    assert [tuple(row) for row in store.conn.execute(
        'SELECT draft_id,selection_state,position,entry_id,video_id FROM road_trip_draft_entries '
        'ORDER BY selection_state,position')] == [tuple(row[index] for index in (0, 1, 2, 4, 5)) for row in selections]
    with store.conn:
        assert not road_trip_draft_storage.save(store.conn, recipe, state, 20,
            expected_revision='worker-input', conditional=True)
    assert road_trip_draft_storage.get(store.conn, recipe) == revised
    with store.read_view() as reopened:
        assert road_trip_draft_storage.get(reopened.conn, recipe) == revised
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_rekey_preserves_unknown_draft_format_verbatim(store):
    store.accounts.upsert_account('Account', 'account.json', None, True)
    recipe = store.road_trip.save_road_trip_recipe(None, 'Future trip', 50, [], [], 60, 1)
    future = {'revision': 'future-worker', 'catalog_state_version': 999, 'keys': ['original'],
              'picks': ['exact-upload'], 'banned': []}
    with store.conn:
        road_trip_draft_storage.save(store.conn, recipe, future, 10)
    before = tuple(store.conn.execute('SELECT * FROM road_trip_drafts').fetchone())
    with store.conn:
        road_trip_draft_documents.rekey(store.conn, {'original': 'replacement'})
    assert tuple(store.conn.execute('SELECT * FROM road_trip_drafts').fetchone()) == before
    assert road_trip_draft_storage.get(store.conn, recipe) == future
