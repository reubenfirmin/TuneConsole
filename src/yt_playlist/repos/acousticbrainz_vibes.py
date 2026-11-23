"""SQL for locally fitted Vibe estimates and their verified inputs."""

from yt_playlist.core import acousticbrainz_projection as estimates
from yt_playlist.core.acousticbrainz_vibe_schema import LINKED_CLASSIFIERS, VERIFIED_MATCH


def matches(conn, keys):
    marks = ','.join('?' for _ in keys)
    return {key: (source, mbid) for key, source, mbid in conn.execute(
        'SELECT r.identity_key,i.source_key,i.mbid FROM recordings r JOIN ('
        'SELECT l.recording_id,l.source_key,lower(s.entity_id) AS mbid FROM rec_musical_links l '
        'JOIN rec_musical_sources s USING(source_key) '
        "WHERE s.provider='acousticbrainz' AND s.scope='recording') i ON i.recording_id=r.id "
        f'WHERE r.identity_key IN ({marks}) AND {VERIFIED_MATCH} ORDER BY i.source_key', keys)}


def training_rows(conn, version):
    # Start with verified provider inputs. There is no need to scan Vibe fits
    # for recordings that couldn't supply a training pair.
    return [tuple(row) for row in conn.execute(
        f'SELECT i.recording_id,i.classifiers,v.vibe,m.score,m.support FROM ({LINKED_CLASSIFIERS}) i '
        'CROSS JOIN recordings r ON r.id=i.recording_id CROSS JOIN recording_vibes v USING(recording_id) '
        'CROSS JOIN measurements m ON m.id=v.measurement_id '
        f"WHERE {VERIFIED_MATCH} AND v.source='audio_measurement' AND m.source='audio_measurement' "
        'AND m.model_version_id=(SELECT id FROM vibe_versions WHERE value=?) AND m.support>0 AND v.vibe IN '
        "('cruise','release','relax','uplift','energize','concentrate') "
        'AND EXISTS (SELECT 1 FROM play_events e WHERE e.recording_id=i.recording_id) '
        'AND EXISTS (SELECT 1 FROM measurement_samples ms JOIN sample_attributes s ON s.id=ms.sample_id '
        "WHERE ms.measurement_id=m.id AND s.recording_id=i.recording_id AND s.file_status='ready') "
        'ORDER BY i.recording_id,v.vibe', (version,))]


def estimate_page(conn, cursor, limit):
    return conn.execute(
        f'WITH page AS (SELECT id AS recording_id,{estimates.COLUMN} FROM recordings '
        f'WHERE id>? AND {estimates.PRESENT} ORDER BY id LIMIT ?) '
        "SELECT page.recording_id,i.classifiers,e.key AS vibe,e.value->>1 AS inputs_version FROM page "
        f'JOIN ({LINKED_CLASSIFIERS}) i USING(recording_id) JOIN json_each(page.{estimates.COLUMN}) e '
        'ORDER BY page.recording_id,e.key', (int(cursor or 0), limit)).fetchall()


def publish(conn, plan):
    conn.executemany('UPDATE rec_musical_links SET acousticbrainz_classifiers=NULL '
                     'WHERE recording_id=? AND acousticbrainz_classifiers IS NOT NULL', plan.deleted_inputs)
    for recording, source, mbid, classifiers in plan.classifier_inputs:
        conn.execute('UPDATE rec_musical_links SET acousticbrainz_classifiers=NULL '
                     'WHERE recording_id=? AND source_key<>? AND acousticbrainz_classifiers IS NOT NULL',
                     (recording, source))
        changed = conn.execute(
            'UPDATE rec_musical_links SET acousticbrainz_classifiers=? WHERE recording_id=? AND source_key=? '
            "AND source_key IN (SELECT source_key FROM rec_musical_sources WHERE lower(entity_id)=? "
            "AND provider='acousticbrainz' AND scope='recording')", (classifiers, recording, source, mbid))
        if changed.rowcount != 1:
            raise ValueError('AcousticBrainz classifier source changed before publication')
    estimates.remove(conn, plan.deleted_estimates)
    for recording, vibe, score, version in plan.estimates:
        estimates.put(conn, recording, vibe, score, version)
