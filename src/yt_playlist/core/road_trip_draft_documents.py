"""Read and rekey draft recovery without admitting new catalog entries."""
from uuid import uuid4

from . import collection_recovery
from .catalog_state import rekey_state


def available(conn):
    return any(row[1] == 'recovery_filename' for row in conn.execute('PRAGMA table_info(road_trip_drafts)'))


def read(conn, row):
    result = collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'],
                                     kind='road-trip-draft', owner=row['id'])
    result['revision'] = row['revision']
    result['picks'], result['banned'] = [], []
    for selection, video in conn.execute('SELECT selection_state,video_id FROM road_trip_draft_entries '
                                       'WHERE draft_id=? ORDER BY selection_state,position', (row['id'],)):
        result['picks' if selection == 'picked' else 'banned'].append(video)
    return result


def rekey(conn, keymap):
    """Rewrite declared recording-key slots; exact upload selections stay intact."""
    conn.execute('UPDATE road_trip_drafts SET revision=revision WHERE 0')
    for row in conn.execute('SELECT * FROM road_trip_drafts').fetchall():
        state = read(conn, row)
        try:
            revised = rekey_state(state, keymap)
        except ValueError:
            # Future recovery formats retain their original document unchanged.
            continue
        if revised == state:
            continue
        revision = revised['revision'] = uuid4().hex
        revised.pop('picks')
        revised.pop('banned')
        filename, digest, size = collection_recovery.publish(conn, 'road-trip-draft', row['id'], revised)
        conn.execute('UPDATE road_trip_drafts SET revision=?,recovery_filename=?,recovery_digest=?,recovery_bytes=? '
                     'WHERE id=? AND revision=?', (revision, filename, digest, size, row['id'], row['revision']))
