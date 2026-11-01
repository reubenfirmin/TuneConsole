"""Publish verified playback attribution and pending album queues independently."""
from .. import album_playback_context, playback_bindings
from ..migrations.runner import MigrationError
from . import playback_context as preparation
from .document_publication import DocumentPublication, main as run


def verify_rows(conn, header, described):
    source, generation = header['source'], header['generation']
    kind, intent, items = described
    if (header['document_kind'], header['item_count']) != (kind, len(items)):
        raise MigrationError('Prepared playback context kind or count differs from its original')
    if source == preparation.BINDINGS_KEY:
        rows = conn.execute('SELECT * FROM '+preparation.BINDINGS+
            ' WHERE source=? AND generation=? ORDER BY position', (source, generation))
        for position, item in enumerate(items):
            opportunity = conn.execute('SELECT CAST(? AS TEXT)', (item['opportunity_id'],)).fetchone()[0]
            expected = (source, generation, position, *preparation._account(conn, item), *preparation._scope(conn, item),
                        *preparation._entry(conn, item['video']), opportunity, str(item['option_id']), float(item['at']))
            row = rows.fetchone()
            if row is None or tuple(row) != expected:
                raise MigrationError('Prepared playback binding differs from its original or references')
        if rows.fetchone() is not None:
            raise MigrationError('Prepared playback context contains extra bindings')
        return
    row = conn.execute('SELECT * FROM '+preparation.INTENTS+' WHERE source=? AND generation=?', (source, generation)).fetchone()
    if intent is None:
        if row is not None:
            raise MigrationError('Cleared playback context contains a prepared intent')
    else:
        album = conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?", (intent['browse'],)).fetchone()
        opportunity, action = conn.execute('SELECT CAST(? AS TEXT),CAST(? AS TEXT)',
            (intent['opportunity_id'], intent.get('action_id'))).fetchone()
        expected = (source, generation, *preparation._account(conn, intent), *preparation._scope(conn, intent),
            intent['browse'], album[0] if album else None, None if album else 'missing_album',
            opportunity, action, float(intent['at']), int('videos' in intent))
        if row is None or tuple(row) != expected:
            raise MigrationError('Prepared album playback intent differs from its original or references')
    rows = conn.execute('SELECT * FROM '+preparation.ITEMS+' WHERE source=? AND generation=? ORDER BY position', (source, generation))
    for position, video in enumerate(items):
        row = rows.fetchone()
        if row is None or tuple(row) != (source, generation, position, *preparation._entry(conn, video)):
            raise MigrationError('Prepared album playback queue differs from its ordered original')
    if rows.fetchone() is not None:
        raise MigrationError('Prepared album playback context contains extra queue members')


OWNERS = {
    'bindings': DocumentPublication(preparation.BINDINGS_KEY, preparation, preparation.BINDINGS_JOB,
                                    playback_bindings.available, verify_rows),
    'pending-album': DocumentPublication(preparation.PENDING_KEY, preparation, preparation.PENDING_JOB,
                                         album_playback_context.available, verify_rows),
}
SOURCES = {owner.source:owner for owner in OWNERS.values()}


def validate_installed(conn):
    for owner in OWNERS.values():
        owner.validate_installed(conn)


def main(argv=None):
    return run(OWNERS, __doc__, argv)


if __name__ == '__main__':
    raise SystemExit(main())
