"""Catalog rekeys preserve exact draft decisions and fence earlier workers."""
from yt_playlist.core import catalog


def test_draft_admission_retains_labels_without_replacing_existing_corrections(store):
    store.accounts.upsert_account('Account', None, True)
    recipe = store.road_trip.save_road_trip_recipe(None, 'Trip', 50, [], [], 60, 1)
    known = store.tracks.upsert_track('known', 'Provider title', 'Provider artist', None, 100)
    with store.conn:
        catalog.choose_field(store.conn, known, 'title', 'My corrected title')
        catalog.choose_field(store.conn, known, 'artist', 'My corrected artist')
    store.road_trip.save_road_trip_draft(recipe, {
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
    store.accounts.upsert_account('Account', None, True)
    recipe = store.road_trip.save_road_trip_recipe(None, 'Trip', 50, [], [], 60, 1)
    first = store.tracks.upsert_track('one', 'One', 'Artist', None, 100)
    second = store.tracks.upsert_track('two', 'Two', 'Artist', None, 100)
    old = store.tracks.catalog_subject(first)['identity_key']
    target = store.tracks.catalog_subject(second)
    state = {'revision': 'worker-input', 'keys': [old, old], 'label': old,
             'pool': [{'key': old, 'identity_key': old, 'video_id': 'one', 'label': old}],
             'picks': ['two', 'one', 'two'], 'banned': ['one'], 'building': True}
    store.road_trip.save_road_trip_draft(recipe, state, 10)
    before = dict(store.conn.execute('SELECT * FROM road_trip_recipes').fetchone())
    store.recording_keys.add_alias(old, target['recording_id'], 'test')
    revised = store.road_trip.get_road_trip_draft(recipe)
    current_key = store.tracks.catalog_subject(first)['identity_key']
    assert current_key != old
    assert revised['keys'] == [current_key, current_key]
    assert revised['pool'][0]['key'] == revised['pool'][0]['identity_key'] == current_key
    assert revised['pool'][0]['video_id'] == 'one'
    assert revised['label'] == revised['pool'][0]['label'] == old
    assert revised['picks'] == state['picks'] and revised['banned'] == state['banned']
    assert revised['revision'] != state['revision']
    after = dict(store.conn.execute('SELECT * FROM road_trip_recipes').fetchone())
    for field in ('id', 'draft_id', 'draft_account_id', 'draft_state', 'draft_updated_at', 'draft_selections'):
        assert after[field] == before[field]
    assert not store.road_trip.replace_road_trip_draft(recipe, state, 20, expected_revision='worker-input')
    assert store.road_trip.get_road_trip_draft(recipe) == revised
    with store.read_view() as reopened:
        assert reopened.road_trip.get_road_trip_draft(recipe) == revised
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_rekey_preserves_unknown_draft_format_verbatim(store):
    store.accounts.upsert_account('Account', None, True)
    recipe = store.road_trip.save_road_trip_recipe(None, 'Future trip', 50, [], [], 60, 1)
    future = {'revision': 'future-worker', 'catalog_state_version': 999, 'keys': ['original'],
              'picks': ['exact-upload'], 'banned': []}
    store.road_trip.save_road_trip_draft(recipe, future, 10)
    before = tuple(store.conn.execute('SELECT * FROM road_trip_recipes').fetchone())
    store.road_trip.rekey_recordings({'original': 'replacement'})
    assert tuple(store.conn.execute('SELECT * FROM road_trip_recipes').fetchone()) == before
    assert store.road_trip.get_road_trip_draft(recipe) == future
