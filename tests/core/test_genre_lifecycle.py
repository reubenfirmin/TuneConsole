"""Current identity edits preserve classification, preferences and source evidence."""
import pytest

from tests.genre_catalog_support import bound_shipped_catalog
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, reuse_reporting_reader, without_query_timing
from yt_playlist.core import audio_vibe_metadata, genre_classifications, genre_identity, metadata, schema_inspection
from yt_playlist.core.store import Store
from yt_playlist.providers import genres
from yt_playlist.rec import album_choices, content_model, group_similarity, radio_genres, transient


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Genre consumers do not construct the web application."""


@pytest.fixture(scope='module')
def genre_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture
def store(genre_schema, monkeypatch, tmp_path):
    # Keep the shipped aliases and families for the identities being edited.
    # Full vocabulary curation is owned by test_genre_catalog.
    bound_shipped_catalog(monkeypatch, ('synth pop', 'drum bass', 'jazz'))
    database = Store(tmp_path / 'genre-lifecycle.db')
    try:
        initialize_consumer(database, genre_schema)
        with database.conn:
            genre_identity.seed_catalog(database.conn)
        with schema_inspection.reuse_tables(database.conn), reuse_reporting_reader(database, monkeypatch):
            yield database
    finally:
        database.conn.close()


def test_rename_family_filter_and_provider_aliases_follow_identity(store):
    store.genres.configure_catalog()
    entry = store.tracks.upsert_track('v','Song','Artist',None,None)
    store.tracks.set_track_genre(entry,'Synth Pop')
    key = store.rec_query.key_for_video('v')
    store.rec_model.set_lean('genre:synth pop',1.4,10)
    store.genres.rename_genre('synth pop','New electronic label')
    assert store.rec_query.track_genres([key]) == {key:'New electronic label'}
    assert store.rec_query.keys_in_genre_selection(['Synth Pop']) == {key}
    family = store.conn.execute("SELECT family_key FROM genre_catalog WHERE key='synth pop'").fetchone()[0]
    assert store.rec_query.keys_in_genre_selection([family]) == {key}
    assert store.rec_model.get_leans() == {'genre:synth pop':1.4}
    assert audio_vibe_metadata.subgenre({'genre':'New electronic label'},conn=store.conn) == 'synth pop'
    genres.configure(store,read_only=True)
    try:
        assert genres.match_tag('Synth Pop') == 'New electronic label'
        assert genres.match_tag('New electronic label') == 'New electronic label'
    finally:
        genres.set_active(None)


def test_merge_keeps_disabled_choice_and_raw_evidence(store):
    store.genres.configure_catalog()
    store.genres.add_genre('Custom style')
    store.genres.remove_genre('Synth Pop')
    entry = store.tracks.upsert_track('v','Song','Artist',None,None)
    with store.conn:
        metadata.select(store.conn,entry,'genre','Custom style','provider',now=10)
    store.rec_model.set_lean('genre:Custom style',1.4,10)
    store.genres.merge_genres('custom style','synth pop',now=20)
    assert store.conn.execute('SELECT genre_key FROM recordings').fetchone()[0] == 'synth pop'
    assert store.conn.execute("SELECT enabled FROM genre_preferences WHERE genre_key='synth pop'").fetchone()[0] == 0
    assert store.rec_model.get_leans() == {'genre:synth pop':1.4}
    assert genre_identity.resolve(store.conn,'Custom style') == ('synth pop',None)
    assert store.conn.execute("SELECT text_value,genre_key FROM metadata_observations WHERE field='genre'").fetchone()[:] == ('Custom style','custom style')
    assert store.conn.execute('SELECT count(*) FROM genre_merge_preferences').fetchone()[0] == 2
    store.genres.configure_catalog()
    assert 'Synth Pop' not in store.genres.get_genre_whitelist()


def test_conflicting_axes_abort_merge_without_discarding_values(store):
    store.genres.configure_catalog()
    store.rec_model.set_lean('genre:synth pop',1.3,10)
    store.rec_model.set_lean('genre:drum bass',1.4,10)
    with pytest.raises(ValueError,match='Conflicting current taste axes'):
        store.genres.merge_genres('synth pop','drum bass')
    assert store.rec_model.get_leans() == {'genre:synth pop':1.3,'genre:drum bass':1.4}
    assert not store.conn.execute('SELECT 1 FROM genre_redirects').fetchone()


def test_alias_edit_does_not_change_existing_selected_key(store):
    store.genres.configure_catalog()
    store.genres.set_genre_alias('A historical label','synth pop')
    entry = store.tracks.upsert_track('v','Song','Artist',None,None)
    with store.conn:
        metadata.select(store.conn,entry,'genre','A historical label','provider',now=10)
    store.genres.set_genre_alias('A historical label','drum bass')
    assert genre_identity.resolve(store.conn,'A historical label') == ('drum bass',None)
    assert store.conn.execute('SELECT genre_key FROM recordings').fetchone()[0] == 'synth pop'
    assert store.conn.execute("SELECT text_value,genre_key FROM metadata_observations WHERE field='genre'").fetchone()[:] == ('A historical label','synth pop')


def test_family_edit_rebuilds_model_inputs_and_live_filters(store):
    entry = store.tracks.upsert_track('v','Song','Artist',None,120)
    store.tracks.set_track_genre(entry,'Synth Pop')
    key = store.rec_query.key_for_video('v')
    inputs = content_model.ContentInputs([key],{key:('Synth-Pop',None)},{})
    original = content_model.input_fingerprint(inputs,genre_axes=store.genres.feature_axes(['Synth-Pop']))
    store.genres.rename_genre('synth pop','New label')
    pin = radio_genres.GenrePolicy(1,[],current={'genre':'New label'},store=store)
    assert pin.allows({'genre':'Synth Pop'})
    assert pin.bonus({'genre':'Synth Pop'}) == 1
    store.genres.set_genre_family('synth pop','jazz')
    axes = store.genres.feature_axes(['Synth-Pop', 'New label'])
    assert content_model.input_fingerprint(inputs,genre_axes=axes) != original
    model = content_model.build_content_model(inputs.content,{},genre_axes=axes)
    assert set(model['cat']) == {'fam:jazz','sub:synth pop'}
    assert content_model.encode_content(model,'New label',None,{},genre_axes=axes) is not None
    facets = transient.facets_for(store,[key])
    assert facets['genre:jazz'] == [key]
    assert facets['genre:synth pop'] == [key]
    assert store.rec_query.keys_in_genre_selection(['jazz']) == {key}
    assert group_similarity.recording(key,{'genre':'New label'},genre_axes=axes).family == 'jazz'
    choices,_ = album_choices.menu([{'browse_id':'release','artist':'Artist','genre':'New label',
                                     'classification':'discovery'}],{'novelty':.5},genres=['jazz'],genre_axes=axes)
    assert [row['browse_id'] for row in choices] == ['release']
    assert store.conn.execute("SELECT revision FROM rec_change_queue WHERE kind='refresh'").fetchone()[0] > 1


def test_detached_family_consumers_use_current_relationships(store):
    from yt_playlist.rec import cluster_explain, recap_story, trend_statistics
    from yt_playlist.rec.road_trip_mix import _is_overlap
    for video,genre in [('electronic','Synth Pop'),('jazz','Jazz')]:
        entry = store.tracks.upsert_track(video,video,'Artist '+video,None,120)
        store.tracks.set_track_genre(entry,genre)
    key = store.tracks.identity_key_for_video('electronic')
    path = store.tracks.identity_key_for_video('jazz')
    assert not any(row['kind']=='genre' for row in cluster_explain.connection_reasons(
        store.cluster_evidence.connection(key,[path])))
    store.genres.set_genre_family('synth pop','jazz')
    assert any(row['kind']=='genre' for row in cluster_explain.connection_reasons(
        store.cluster_evidence.connection(key,[path])))
    axes = store.genres.feature_axes(['Synth Pop','Jazz'])
    meta = {key:('Artist','Synth Pop')}
    month = trend_statistics.compute_months([(1,key,2)],meta,{},genre_axes=axes)[0]
    assert month['families']=={'jazz':2}
    assert recap_story.energy_axis({key:2},{},meta,genre_axes=axes)==(.3,'genre')
    assert _is_overlap({'artist':'Other','genre':'Synth Pop','family':'jazz'},
        {'inputs':{'genres':['Jazz']},'genre_families':{g:row[0] for g,row in axes.items()}})


@pytest.mark.parametrize('operation', ['replay', 'transfer'])
def test_retained_genre_evidence_keeps_captured_identity_after_alias_reassignment(store, operation):
    store.genres.configure_catalog()
    label = 'A retained provider label'
    store.genres.set_genre_alias(label, 'synth pop')
    entry = store.tracks.upsert_track('retained', 'Original song', 'Artist', None, 200)
    with store.conn:
        metadata.select(store.conn, entry, 'genre', label, 'provider', now=10)
    recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()[0]
    original = store.conn.execute(
        "SELECT o.* FROM recording_metadata_selections s JOIN metadata_observations o ON o.id=s.observation_id "
        "WHERE s.recording_id=? AND s.field='genre'", (recording,)).fetchone()
    assert (original['text_value'], original['genre_key']) == (label, 'synth pop')
    original_row = tuple(original)

    store.genres.set_genre_alias(label, 'drum bass')
    assert genre_identity.resolve(store.conn, label) == ('drum bass', None)
    # New evidence must use the new alias; only the retained capture keeps its old identity.
    fresh_entry = store.tracks.upsert_track('fresh', 'Fresh song', 'Artist', None, 200)
    with store.conn:
        metadata.select(store.conn, fresh_entry, 'genre', label, 'provider', now=20)
    assert store.conn.execute(
        'SELECT r.genre_key FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id WHERE e.id=?',
        (fresh_entry,)).fetchone()[0] == 'drum bass'

    target_entry, target_recording = entry, recording
    if operation == 'replay':
        with store.conn:
            metadata.select(store.conn, entry, 'genre', label, 'provider', now=30,
                            observation_id=original['id'])
    else:
        # The production transfer boundary receives a verified-equivalent target recording.
        target_entry = store.tracks.upsert_track('equivalent', 'Equivalent song', 'Artist', None, 200)
        target_recording = store.conn.execute(
            'SELECT recording_id FROM catalog_entries WHERE id=?', (target_entry,)).fetchone()[0]
        assert target_recording != recording
        with store.conn:
            store.conn.execute('BEGIN')
            metadata.reuse_recording_selections(store.conn, recording, target_recording)

    selected = store.conn.execute(
        "SELECT o.* FROM recording_metadata_selections s JOIN metadata_observations o ON o.id=s.observation_id "
        "WHERE s.recording_id=? AND s.field='genre'", (target_recording,)).fetchone()
    assert selected is not None
    assert (selected['text_value'], selected['genre_key']) == (label, 'synth pop')
    if operation == 'replay':
        assert selected['id'] == original['id']
    else:
        assert selected['origin'] == 'selection_transfer'
        assert selected['derived_from_observation_id'] == original['id']
        assert (selected['entry_id'], selected['recording_id']) == (target_entry, target_recording)
    assert tuple(store.conn.execute('SELECT * FROM metadata_observations WHERE id=?',
                                    (original['id'],)).fetchone()) == original_row
    classification = tuple(store.conn.execute(
        'SELECT genre_key,unresolved_genre FROM recordings WHERE id=?', (target_recording,)).fetchone())
    assert classification == ('synth pop', None), {'operation': operation, 'classification': classification,
                                                 'selected_genre_key': selected['genre_key']}
    expected_label = store.conn.execute("SELECT name FROM genre_catalog WHERE key='synth pop'").fetchone()[0]
    assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (target_entry,)).fetchone()[0] == expected_label


@pytest.mark.parametrize('operation', ['replay', 'transfer'])
def test_retained_genre_evidence_follows_chained_merges_without_rewriting_capture(store, operation):
    store.genres.configure_catalog()
    for label in ('Captured original style', 'Merged middle style', 'Merged final style'):
        store.genres.add_genre(label)
    entry = store.tracks.upsert_track('merged-genre', 'Original song', 'Artist', None, 200)
    with store.conn:
        metadata.select(store.conn, entry, 'genre', 'Captured original style', 'provider', now=10)
    recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()[0]
    original = store.conn.execute(
        "SELECT o.* FROM recording_metadata_selections s JOIN metadata_observations o ON o.id=s.observation_id "
        "WHERE s.recording_id=? AND s.field='genre'", (recording,)).fetchone()
    assert (original['text_value'], original['genre_key']) == ('Captured original style', 'captured original style')
    original_row = tuple(original)

    store.genres.merge_genres('captured original style', 'merged middle style', now=20)
    store.genres.merge_genres('merged middle style', 'merged final style', now=21)
    assert genre_identity.resolve(store.conn, 'Captured original style') == ('merged final style', None)
    target_entry, target_recording = entry, recording
    if operation == 'replay':
        with store.conn:
            metadata.select(store.conn, entry, 'genre', 'Captured original style', 'provider', now=30,
                            observation_id=original['id'])
    else:
        target_entry = store.tracks.upsert_track('merged-equivalent', 'Equivalent song', 'Artist', None, 200)
        target_recording = store.conn.execute(
            'SELECT recording_id FROM catalog_entries WHERE id=?', (target_entry,)).fetchone()[0]
        assert target_recording != recording
        with store.conn:
            store.conn.execute('BEGIN')
            metadata.reuse_recording_selections(store.conn, recording, target_recording)

    selected = store.conn.execute(
        "SELECT o.* FROM recording_metadata_selections s JOIN metadata_observations o ON o.id=s.observation_id "
        "WHERE s.recording_id=? AND s.field='genre'", (target_recording,)).fetchone()
    assert (selected['text_value'], selected['genre_key']) == ('Captured original style', 'captured original style')
    if operation == 'replay':
        assert selected['id'] == original['id']
    else:
        assert selected['origin'] == 'selection_transfer'
        assert selected['derived_from_observation_id'] == original['id']
        assert (selected['entry_id'], selected['recording_id']) == (target_entry, target_recording)
    assert tuple(store.conn.execute('SELECT * FROM metadata_observations WHERE id=?',
                                    (original['id'],)).fetchone()) == original_row
    assert tuple(store.conn.execute('SELECT genre_key,unresolved_genre FROM recordings WHERE id=?',
                                    (target_recording,)).fetchone()) == ('merged final style', None)
    assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?',
                              (target_entry,)).fetchone()[0] == 'Merged final style'


@pytest.mark.parametrize('operation', ['replay', 'transfer'])
def test_retained_unresolved_genre_is_not_reinterpreted_by_a_new_alias(store, operation):
    store.genres.configure_catalog()
    label = 'An unresolved historical provider label'
    entry = store.tracks.upsert_track('unknown-genre', 'Original song', 'Artist', None, 200)
    with store.conn:
        metadata.select(store.conn, entry, 'genre', label, 'provider', now=10)
    original = store.conn.execute(
        "SELECT o.* FROM recording_metadata_selections s JOIN metadata_observations o ON o.id=s.observation_id "
        "JOIN catalog_entries e ON e.recording_id=s.recording_id WHERE e.id=? AND s.field='genre'",
        (entry,)).fetchone()
    assert original['genre_key'] is None
    assert original['text_value'] == label
    original_row = tuple(original)

    store.genres.set_genre_alias(label, 'synth pop')
    fresh = store.tracks.upsert_track('new-genre', 'Fresh song', 'Artist', None, 200)
    with store.conn:
        metadata.select(store.conn, fresh, 'genre', label, 'provider', now=20)
    assert store.conn.execute(
        'SELECT r.genre_key FROM recordings r JOIN catalog_entries e ON e.recording_id=r.id WHERE e.id=?',
        (fresh,)).fetchone()[0] == 'synth pop'

    target_entry, target_recording = entry, original['recording_id']
    if operation == 'replay':
        with store.conn:
            metadata.select(store.conn, entry, 'genre', label, 'provider', now=30,
                            observation_id=original['id'])
    else:
        target_entry = store.tracks.upsert_track('unresolved-equivalent', 'Equivalent song', 'Artist', None, 200)
        target_recording = store.conn.execute(
            'SELECT recording_id FROM catalog_entries WHERE id=?', (target_entry,)).fetchone()[0]
        assert target_recording != original['recording_id']
        with store.conn:
            store.conn.execute('BEGIN')
            metadata.reuse_recording_selections(store.conn, original['recording_id'], target_recording)
    selected = store.conn.execute(
        "SELECT o.* FROM recording_metadata_selections s JOIN metadata_observations o ON o.id=s.observation_id "
        "WHERE s.recording_id=? AND s.field='genre'", (target_recording,)).fetchone()
    assert (selected['text_value'], selected['genre_key'], selected['resolution_reason']) == (
        label, None, original['resolution_reason'])
    if operation == 'replay':
        assert selected['id'] == original['id']
    else:
        assert selected['origin'] == 'selection_transfer'
        assert selected['derived_from_observation_id'] == original['id']
        assert (selected['entry_id'], selected['recording_id']) == (target_entry, target_recording)
    assert tuple(store.conn.execute('SELECT * FROM metadata_observations WHERE id=?',
                                    (original['id'],)).fetchone()) == original_row
    assert tuple(store.conn.execute(
        'SELECT r.genre_key,r.unresolved_genre FROM recordings r '
        'JOIN catalog_entries e ON e.recording_id=r.id WHERE e.id=?', (target_entry,)).fetchone()) == (None, label)
    assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (target_entry,)).fetchone()[0] == label


def test_legacy_unattributed_genre_transfer_preserves_unresolved_label_without_new_evidence(store):
    store.genres.configure_catalog()
    label = 'An unattributed historical style'
    entry = store.tracks.upsert_track('legacy-genre', 'Original song', 'Artist', None, 200)
    target_entry = store.tracks.upsert_track('legacy-equivalent', 'Equivalent song', 'Artist', None, 200)
    previous = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()[0]
    target = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (target_entry,)).fetchone()[0]
    assert previous != target
    # A migrated base value can have known text and no provable supplying observation.
    with store.conn:
        genre_classifications.write(store.conn, 'recordings', previous, label)
        store.conn.execute('INSERT INTO recording_metadata_selections('
            'recording_id,field,attribution_status,legacy_source,selected_at) '
            "VALUES (?,'genre','legacy_unresolved','old_provider',10)", (previous,))
    original = tuple(store.conn.execute(
        "SELECT * FROM recording_metadata_selections WHERE recording_id=? AND field='genre'", (previous,)).fetchone())
    store.genres.set_genre_alias(label, 'synth pop')
    assert genre_identity.resolve(store.conn, label) == ('synth pop', None)
    with store.conn:
        store.conn.execute('BEGIN')
        metadata.reuse_recording_selections(store.conn, previous, target)
    assert tuple(store.conn.execute('SELECT genre_key,unresolved_genre FROM recordings WHERE id=?',
                                    (target,)).fetchone()) == (None, label)
    assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (target_entry,)).fetchone()[0] == label
    assert tuple(store.conn.execute('SELECT observation_id,attribution_status,legacy_source,selected_at '
        "FROM recording_metadata_selections WHERE recording_id=? AND field='genre'", (target,)).fetchone()) == (
            None, 'legacy_unresolved', 'old_provider', 10)
    assert tuple(store.conn.execute(
        "SELECT * FROM recording_metadata_selections WHERE recording_id=? AND field='genre'", (previous,)).fetchone()) == original
    assert tuple(store.conn.execute('SELECT genre_key,unresolved_genre FROM recordings WHERE id=?',
                                    (previous,)).fetchone()) == (None, label)
    assert store.conn.execute("SELECT count(*) FROM metadata_observations WHERE field='genre' "
                              'AND recording_id IN (?,?)', (previous, target)).fetchone()[0] == 0
