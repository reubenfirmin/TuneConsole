"""Remember the preferred playable upload on its track source."""
import json
from functools import partial

from .. import catalog, discovery_provenance, track_sources
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'own_playback_preference_on_track_sources'
RETIRED = 'youtube_playback_alternates'
SOURCE = 'track_sources'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        'SELECT sql FROM sqlite_schema WHERE name IN (?,?) ORDER BY type,name',
        (SOURCE, RETIRED))]


def _save_association(conn, video, recording, previous, source, evidence, at):
    return conn.execute('INSERT INTO item_recording_associations(video_id,recording_id,previous_recording_id,'
                        'source,evidence,at) VALUES (?,?,?,?,?,?)',
                        (video, recording, previous, source, evidence, at)).lastrowid


def _refresh_offers(conn, _kind, recording):
    conn.execute('UPDATE recordings SET (discovery_offered_count,discovery_first_shown,discovery_last_shown)='
                 '(SELECT count(*),min(shown_at),max(shown_at) FROM discovery_impressions WHERE recording_id=?) '
                 'WHERE id=? AND discovery_entry_id IS NOT NULL', (recording, recording))


def _merge_offers(conn, kind, previous, target):
    conn.execute('INSERT INTO discovery_impressions(view_id,recording_id,shown_at) '
                 'SELECT view_id,?,shown_at FROM discovery_impressions WHERE recording_id=? '
                 'ON CONFLICT(view_id,recording_id) WHERE recording_id IS NOT NULL DO UPDATE SET '
                 'shown_at=min(shown_at,excluded.shown_at)', (target, previous))
    conn.execute('DELETE FROM discovery_impressions WHERE recording_id=?', (previous,))
    discovery_provenance.merge_earlier_offers(conn, kind, previous, target)
    _refresh_offers(conn, kind, target)


def _rekey_impressions(conn, old_key, new_key):
    conn.execute('INSERT INTO rec_impressions SELECT surface,?,views,last_shown FROM rec_impressions '
        'WHERE item_key=? ON CONFLICT(surface,item_key) DO UPDATE SET views=views+excluded.views,'
        'last_shown=MAX(COALESCE(last_shown,0),COALESCE(excluded.last_shown,0))', (new_key, old_key))
    conn.execute('DELETE FROM rec_impressions WHERE item_key=?', (old_key,))


def install(conn, _context):
    conn.execute(f'ALTER TABLE {SOURCE} ADD COLUMN {track_sources.PLAYBACK_PREFERENCE}')
    verified = conn.execute(f'''
        SELECT requested_video_id,video_id,evidence_json,observed_at
        FROM {RETIRED} WHERE same_recording=1
        ORDER BY observed_at,account_id,requested_video_id
    ''').fetchall()
    finish_discovery = partial(discovery_provenance.finish_reassociation, merge_offers=_merge_offers,
                               refresh_offers=_refresh_offers)
    for requested, actual, raw_evidence, observed_at in verified:
        target, = conn.execute('SELECT recording_id FROM track_sources WHERE video_id=?', (requested,)).fetchone()
        evidence = json.loads(raw_evidence)
        catalog.associate_item(conn, actual, target, source='youtube_playback_substitution',
                               evidence={**evidence, 'requested_video_id': requested}, now=observed_at,
                               same_recording=True, save_association=_save_association, finish_discovery=finish_discovery,
                               rekey_impressions=_rekey_impressions)
        track_sources.prefer_playback(conn, target, actual)
    conn.execute(f'DROP TABLE {RETIRED}')


def validate(conn):
    installed = definitions(conn, (SOURCE, RETIRED))
    if set(installed) != {SOURCE} or track_sources.PLAYBACK_PREFERENCE not in installed[SOURCE]:
        raise MigrationError('Playback preference was not folded into track sources')
