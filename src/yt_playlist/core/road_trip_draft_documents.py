"""Read and rekey draft recovery without admitting new catalog entries."""
from uuid import uuid4

from . import collection_recovery
from .catalog_state import rekey_state


def rekey(conn, keymap):
    """Rewrite declared recording-key slots; exact upload selections stay intact."""
    conn.execute('UPDATE road_trip_recipes SET draft_revision=draft_revision WHERE 0')
    for owner, previous, filename, digest, size in conn.execute(
        'SELECT draft_id,draft_revision,recovery_filename,recovery_digest,recovery_bytes '
        'FROM road_trip_recipes WHERE draft_id IS NOT NULL'
    ).fetchall():
        state = collection_recovery.read(filename, digest, size, kind='road-trip-draft', owner=owner)
        try:
            revised = rekey_state(state, keymap)
        except ValueError:
            # Future recovery formats retain their original document unchanged.
            continue
        if revised == state:
            continue
        revision = revised['revision'] = uuid4().hex
        reference = collection_recovery.publish(conn, 'road-trip-draft', owner, revised)
        conn.execute('UPDATE road_trip_recipes SET draft_revision=?,recovery_filename=?,recovery_digest=?,'
                     'recovery_bytes=? WHERE draft_id=? AND draft_revision=?', (revision, *reference, owner, previous))
