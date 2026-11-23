"""Prepare the same retained profile inputs and bounded vibe pools as the worker."""
from uuid import NAMESPACE_URL, uuid5

from tests.recommendation_support import publish_bundles
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.rec import musical_preparation, musical_profiles
from yt_playlist.rec.progressive import Budget, Preparation


def record_acousticbrainz(store, entry, inputs, *, now=1000):
    with store.conn:
        recording, mbid = store.conn.execute(
            'SELECT r.id,r.mb_recording_id FROM recordings r JOIN track_sources e ON e.recording_id=r.id '
            'WHERE e.id=?', (entry,)).fetchone()
        if mbid is None:
            mbid = str(uuid5(NAMESPACE_URL, f'tuneconsole-test:{recording}'))
            store.conn.execute('UPDATE recordings SET mb_recording_id=? WHERE id=?', (mbid, recording))
            store.conn.execute('INSERT INTO recording_identifiers(recording_id,scheme,entity_type,value,source,status) '
                               "VALUES (?,'musicbrainz','recording',?,'musicbrainz','accepted')", (recording, mbid))
    store.enrichment_state.record_metadata_observations(entry, 'acousticbrainz', [
        MetadataObservation(field, value, 'recording', mbid, 'prediction', unit='classifier_probability',
                            match_evidence={'method': 'supplied_recording_mbid'})
        for field, value in inputs.items()], run_id=f'{entry}:{now}', now=now)


def prepare_profiles(store, now=1000):
    # Consumers need all completed profiles, rather than a particular number
    # of worker turns. Keep every recording and real guarded publication while
    # processing this controlled input in larger, bounded batches.
    preparation = Preparation(store, budget=Budget(batch=512))
    try:
        for _ in range(500):
            if not store.prepared_content.has_pending() and not musical_preparation.due(store):
                return
            # A bounded capture may defer without completing a batch. Continue
            # until the actual work drains, with the same finite attempt bound.
            preparation.capture(now)
        raise AssertionError('Profile preparation did not drain: '
            f'pending={store.prepared_content.pending(32)!r}, '
            f'musical_due={musical_preparation.due(store)!r}')
    finally:
        preparation.close()


def prepare_vibes(store, now=1000):
    prepare_profiles(store, now)
    bundles = getattr(store, '_fixture_bundle_payload', None)
    if bundles is None:
        bundles = store.rec_proposals.get_proposals('mode_bundles') or {}
    publish_bundles(store, bundles, now, vibes=True)
    return bundles


def publish_intensities(store, values, now=1000):
    """Publish controlled completed profiles for tests of their consumers."""
    from yt_playlist.core.audio_vibe_contract import INTENSITY_ENERGIZE

    rows = []
    for key, intensity in values.items():
        profile = musical_profiles.resolve([], overrides={'intensity': intensity})
        rows.append((key, musical_profiles.canonical(profile), profile['version'],
                     musical_profiles.digest(profile)))
    store.musical_profiles.publish_recordings(rows, now)
    publish_measured_vibes(store, INTENSITY_ENERGIZE, values, now)


def publish_measured_vibes(store, vibe, values, now):
    """Record qualified measurements and the current fits consumed by serving."""
    from yt_playlist.core import vibe_versions
    from yt_playlist.rec.vibe_recording_rules import version

    with store.conn:
        measurement = store.conn.execute('SELECT coalesce(max(id),0)+1 FROM measurements').fetchone()[0]
        stamp = version()
        version_id = vibe_versions.intern(store.conn, stamp)
        for index, (key, score) in enumerate(values.items()):
            recording = store.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()[0]
            identity = measurement + index
            store.conn.execute('INSERT INTO measurements(id,recording_id,vibe,score,support,source,'
                'model_version_id,input_digest,measured_at) VALUES (?,?,?,?,?,?,?,?,?)', (
                identity, recording, vibe, score, 1., 'audio_measurement',
                version_id, musical_profiles.digest([vibe, score, now]), now))
            store.conn.execute('INSERT INTO recording_vibes(recording_id,vibe,score,support,source,measurement_id,'
                'inputs_version_id) VALUES (?,?,?,?,?,?,?) ON CONFLICT(recording_id,vibe) DO UPDATE SET '
                'score=excluded.score,support=excluded.support,source=excluded.source,measurement_id=excluded.measurement_id,'
                'prediction_id=NULL,inputs_version_id=excluded.inputs_version_id',
                (recording, vibe, score, 1., 'audio_measurement', identity, version_id))
