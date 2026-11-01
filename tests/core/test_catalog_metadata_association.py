"""Recording fact reuse preserves selected provenance without moving historical evidence."""
import pytest

from yt_playlist.core import catalog, genre_classifications, metadata


def pair(store):
    old = store.tracks.upsert_track('old', 'Old', 'Artist', 'Old release', 200)
    target = store.tracks.upsert_track('target', 'Target', 'Artist', 'Target release', 210)
    return old, target, store.tracks.catalog_subject(target)['recording_id']


def associate(store, recording, *, equivalent=True):
    store.tracks.associate_youtube_item('old', recording, source='review',
        evidence={'review': 'independent identity check'}, now=100, same_recording=equivalent)


def test_equivalent_recording_copies_selected_value_source_and_time(store):
    old, target, recording = pair(store)
    previous = store.tracks.catalog_subject(old)['recording_id']
    with store.conn:
        metadata.apply_provider(store.conn, old, 'first', {'genre': 'House'}, ['first', 'second'])
        metadata.select(store.conn, old, 'genre', 'House', 'first', now=42)
    history = list(map(tuple, store.conn.execute('SELECT * FROM metadata_observations WHERE recording_id=?',
                                                 (previous,))))
    supplied = store.conn.execute("SELECT observation_id FROM recording_metadata_selections "
                                  "WHERE recording_id=? AND field='genre'",(previous,)).fetchone()[0]
    associate(store, recording)
    assert genre_classifications.read(store.conn,'recordings',recording) == 'House'
    assert metadata.selected_source(store.conn,target,'genre')=='first'
    assert tuple(store.conn.execute('SELECT parent.id,parent.observed_at FROM recording_metadata_selections s '
        'JOIN metadata_observations child ON child.id=s.observation_id '
        'JOIN metadata_observations parent ON parent.id=child.derived_from_observation_id '
        "WHERE s.recording_id=? AND s.field='genre'",(recording,)).fetchone())==(supplied,42)
    with store.conn:
        metadata.apply_provider(store.conn, target, 'second', {'genre': 'Jazz'}, ['first', 'second'])
    assert store.tracks.get_track_enrichment(target)[0] == 'House'
    assert list(map(tuple, store.conn.execute('SELECT * FROM metadata_observations WHERE recording_id=?',
                                             (previous,)))) == history
    assert store.conn.execute('SELECT album,duration_s FROM catalog_tracks WHERE id=?',
                               (old,)).fetchone()[:] == ('Old release', 200)


@pytest.mark.parametrize('value', ['Jazz', '', None])
def test_target_selection_and_item_overrides_survive_equivalence(store, value):
    old, target, recording = pair(store)
    with store.conn:
        metadata.select(store.conn, old, 'genre', 'House', 'first', now=41)
        metadata.select(store.conn, target, 'genre', value, 'target-provider', now=43)
    store.tracks.set_track_genre(old, '')
    with store.conn:
        catalog.choose_field(store.conn, target, 'genre', None, now=44)
    decisions = list(map(tuple, store.conn.execute('SELECT * FROM metadata_decisions')))
    associate(store, recording)
    assert genre_classifications.read(store.conn,'recordings',recording) == value
    assert metadata.selected_source(store.conn,target,'genre')=='target-provider'
    assert store.conn.execute('SELECT selected_at FROM recording_metadata_selections '
        "WHERE recording_id=? AND field='genre'", (recording,)).fetchone()[0] == 43
    assert store.tracks.get_track_enrichment(old)[0] == ''
    assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (target,)).fetchone()[0] is None
    assert list(map(tuple, store.conn.execute('SELECT * FROM metadata_decisions'))) == decisions


def test_correction_does_not_reuse_old_recording_facts(store):
    old, target, recording = pair(store)
    with store.conn:
        metadata.select(store.conn, old, 'genre', 'House', 'first', now=42)
    associate(store, recording, equivalent=False)
    assert genre_classifications.read(store.conn,'recordings',recording) is None
    assert metadata.selected_source(store.conn, target, 'genre') is None


def test_association_failure_rolls_back_value_and_provenance_together(store, monkeypatch):
    old, target, recording = pair(store)
    previous = store.tracks.catalog_subject(old)['recording_id']
    with store.conn:
        metadata.select(store.conn, old, 'genre', 'House', 'first', now=42)
    original = metadata.reuse_recording_selections

    def fail(conn, before, after):
        original(conn, before, after)
        raise RuntimeError('after selection copy')

    monkeypatch.setattr(metadata, 'reuse_recording_selections', fail)
    associations = list(map(tuple, store.conn.execute('SELECT * FROM item_recording_associations')))
    with pytest.raises(RuntimeError, match='after selection copy'):
        associate(store, recording)
    assert store.tracks.catalog_subject(old)['recording_id'] == previous
    assert genre_classifications.read(store.conn,'recordings',recording) is None
    assert metadata.selected_source(store.conn, target, 'genre') is None
    assert metadata.selected_source(store.conn, old, 'genre') == 'first'
    assert list(map(tuple, store.conn.execute('SELECT * FROM item_recording_associations'))) == associations
