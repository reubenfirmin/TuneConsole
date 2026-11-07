"""Catalog rekeys preserve exact draft decisions and fence earlier workers."""
from yt_playlist.core import catalog, road_trip_draft_storage


def test_draft_admission_retains_labels_without_replacing_existing_corrections(store):
    store.accounts.upsert_account('Account', 'account.json', None, True)
    recipe = store.road_trip.save_road_trip_recipe(None, 'Trip', 50, [], [], 60, 1)
    known = store.tracks.upsert_track('known', 'Provider title', 'Provider artist', None, 100)
    with store.conn:
        catalog.choose_field(store.conn, known, 'title', 'My corrected title')
        catalog.choose_field(store.conn, known, 'artist', 'My corrected artist')
        road_trip_draft_storage.save(store.conn, recipe, {
            'revision': 'selected', 'picks': ['known', 'passenger'], 'banned': [],
            'pool': [{'video_id': 'known', 'title': 'Provider title', 'artist': 'Provider artist'},
                     {'video_id': 'passenger', 'title': 'Passenger title', 'artist': 'Passenger artist'}],
        }, 10)
    assert [tuple(row) for row in store.conn.execute(
        'SELECT video_id,title,artist FROM catalog_tracks ORDER BY video_id')] == [
            ('known', 'My corrected title', 'My corrected artist'),
            ('passenger', 'Passenger title', 'Passenger artist')]
    assert store.tracks.identity_key_for_video('passenger') == '@unresolved-video:passenger'
    assert not store.conn.execute("SELECT 1 FROM recording_aliases WHERE alias='passenger title|passenger artist'").fetchone()
    # The later provider read can select its metadata; the draft must not have
    # established blank selected values that prevent normal catalog admission.
    passenger = store.tracks.upsert_track('passenger', 'Passenger title', 'Passenger artist', None, 245)
    assert tuple(store.conn.execute('SELECT title,artist,duration_s FROM catalog_tracks WHERE id=?',
                                   (passenger,)).fetchone()) == ('Passenger title', 'Passenger artist', 245)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


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
    store.recording_keys.add_alias(old, target['recording_id'], 'test')
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
    store.road_trip.rekey_recordings({'original': 'replacement'})
    assert tuple(store.conn.execute('SELECT * FROM road_trip_drafts').fetchone()) == before
    assert road_trip_draft_storage.get(store.conn, recipe) == future
