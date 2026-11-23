"""Recording fact reuse preserves selected provenance without moving historical evidence."""
import sqlite3

import pytest

from yt_playlist.core import catalog, genre_classifications, metadata


def pair(store):
    old = store.tracks.upsert_track('old', 'Old', 'Artist', 'Old release', 200)
    target = store.tracks.upsert_track('target', 'Target', 'Artist', 'Target release', 210)
    return old, target, store.tracks.catalog_subject(target)['recording_id']


def associate(store, recording, *, equivalent=True):
    store.tracks.associate_youtube_item('old', recording, source='review',
        evidence={'review': 'independent identity check'}, now=100, same_recording=equivalent)


def test_title_correction_is_recording_owned_and_reset_restores_provider_value(store):
    first, alternate, recording = pair(store)
    associate(store, recording)
    conn = store.conn

    def titles():
        stored = conn.execute('SELECT effective_title FROM recordings WHERE id=?', (recording,)).fetchone()[0]
        tracks = conn.execute('SELECT title,orig_title,duration_s FROM catalog_tracks '
                              'WHERE recording_id=? ORDER BY id', (recording,)).fetchall()
        return stored, list(map(tuple, tracks))

    store.metadata_choices.choose(alternate, 'title', 'Corrected', now=101)
    assert titles() == ('Corrected', [('Corrected', 'Old', 200), ('Corrected', 'Target', 210)])
    assert metadata.has_choice(conn, first, 'title')
    with conn:
        metadata.select(conn, first, 'title', 'Updated by provider', 'musicbrainz', now=102)
    assert titles()[0] == 'Corrected'

    conn.execute("CREATE TEMP TRIGGER reject_edit_history BEFORE UPDATE OF metadata_corrections ON recordings "
                 "WHEN (NEW.metadata_corrections->'$[#-1]')->>4='edit' "
                 "BEGIN SELECT RAISE(ABORT,'unavailable audit'); END")
    with pytest.raises(sqlite3.IntegrityError, match='unavailable audit'):
        store.metadata_choices.choose(first, 'title', 'Unrecorded', now=103)
    assert titles()[0] == 'Corrected'
    with conn:
        catalog.withdraw_choice(conn, first, 'title', now=104)
    assert titles() == ('Updated by provider',
                        [('Updated by provider', 'Old', 200), ('Updated by provider', 'Target', 210)])
    assert not metadata.has_choice(conn, alternate, 'title')
    assert tuple(conn.execute("SELECT operation,value FROM recording_metadata_history WHERE field='title' "
                              'AND recording_id=? ORDER BY id DESC LIMIT 1', (recording,)).fetchone()) == (
        'reset', 'Updated by provider',
    )


def test_equivalent_recording_copies_selected_value_source_and_time(store):
    old, target, recording = pair(store)
    previous = store.tracks.catalog_subject(old)['recording_id']
    with store.conn:
        metadata.apply_provider(store.conn, old, 'first', {'genre': 'House'}, ['first', 'second'])
        metadata.select(store.conn, old, 'genre', 'House', 'first', now=42)
    history = list(map(tuple, store.conn.execute('SELECT * FROM metadata_observations WHERE recording_id=?',
                                                 (previous,))))
    supplied = store.conn.execute("SELECT observation_id FROM current_metadata_selections "
                                  "WHERE recording_id=? AND field='genre'",(previous,)).fetchone()[0]
    associate(store, recording)
    assert genre_classifications.read(store.conn,'recordings',recording) == 'House'
    assert metadata.selected_source(store.conn,target,'genre')=='first'
    assert tuple(store.conn.execute('SELECT parent.id,parent.observed_at FROM current_metadata_selections s '
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
    assert store.conn.execute('SELECT selected_at FROM current_metadata_selections '
        "WHERE recording_id=? AND field='genre'", (recording,)).fetchone()[0] == 43
    assert store.tracks.get_track_enrichment(old)[0] == ''
    assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (target,)).fetchone()[0] is None
    assert list(map(tuple, store.conn.execute('SELECT * FROM metadata_decisions'))) == decisions


def test_correction_does_not_reuse_old_recording_facts(store):
    old, target, recording = pair(store)
    with store.conn:
        metadata.select(store.conn, old, 'genre', 'House', 'first', now=42)
    store.metadata_choices.choose(old, 'genre', 'Techno', now=43)
    associate(store, recording, equivalent=False)
    assert genre_classifications.read(store.conn,'recordings',recording) is None
    assert metadata.selected_source(store.conn, target, 'genre') is None
    assert store.tracks.get_track_enrichment(target)[0] == ''
    assert not metadata.has_choice(store.conn, target, 'genre')


def test_completed_metadata_reuse_survives_a_later_failure_and_association_can_retry(store, monkeypatch):
    old, target, recording = pair(store)
    previous = store.tracks.catalog_subject(old)['recording_id']
    with store.conn:
        metadata.select(store.conn, old, 'genre', 'House', 'first', now=42)
    original = store.metadata_choices.reuse_recording_selections

    def fail(before, after):
        assert not store.conn.in_transaction
        original(before, after)
        raise RuntimeError('after selection copy')

    monkeypatch.setattr(store.metadata_choices, 'reuse_recording_selections', fail)
    history = 'SELECT id,association_history FROM track_sources ORDER BY id'
    associations = list(map(tuple, store.conn.execute(history)))
    with pytest.raises(RuntimeError, match='after selection copy'):
        associate(store, recording)
    assert store.tracks.catalog_subject(old)['recording_id'] == previous
    assert genre_classifications.read(store.conn,'recordings',recording) == 'House'
    assert metadata.selected_source(store.conn, target, 'genre') == 'first'
    assert metadata.selected_source(store.conn, old, 'genre') == 'first'
    assert list(map(tuple, store.conn.execute(history))) == associations
    selection = store.conn.execute("SELECT observation_id FROM current_metadata_selections WHERE recording_id=? AND field='genre'",
        (recording,)).fetchone()[0]
    monkeypatch.setattr(store.metadata_choices, 'reuse_recording_selections', original)
    associate(store, recording)
    assert store.tracks.catalog_subject(old)['recording_id'] == recording
    assert store.conn.execute("SELECT observation_id FROM current_metadata_selections WHERE recording_id=? AND field='genre'",
        (recording,)).fetchone()[0] == selection
    assert not store.conn.in_transaction
