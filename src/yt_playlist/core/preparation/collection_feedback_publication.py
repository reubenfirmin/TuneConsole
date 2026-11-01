"""Publish verified album judgments and authentication flags independently."""
from .. import account_auth_state, album_feedback_storage, entity_names
from ..migrations.runner import MigrationError
from . import collection_feedback as preparation
from .document_publication import DocumentPublication, main as run


def verify_rows(conn, header, items):
    source, generation = header['source'], header['generation']
    if header['item_count'] != len(items):
        raise MigrationError('Prepared collection feedback count differs from its original')
    owner = preparation.RESPONSES if source == 'album_feedback' else preparation.AUTH
    rows = conn.execute('SELECT * FROM '+owner+' WHERE source=? AND generation=? ORDER BY position',
                        (source, generation))
    for position, (key, value) in enumerate(items):
        if source == 'album_feedback':
            album = conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?", (key,)).fetchone()
            # Match SQLite's TEXT affinity for historical numeric/bool IDs while
            # the retained original keeps their exact JSON kinds and spelling.
            opportunity, action = conn.execute('SELECT CAST(? AS TEXT),CAST(? AS TEXT)',
                (value.get('opportunity_id'), value.get('action_id'))).fetchone()
            expected = (source, generation, position, key, album[0] if album else None,
                None if album else 'missing_album', value['kind'], float(value['at']), opportunity, action, None, 1)
        else:
            account = conn.execute('SELECT id FROM '+entity_names.accounts(conn)+' WHERE id=?', (int(key),)).fetchone()
            expected = (source, generation, position, key, account[0] if account else None,
                        None if account else int(key), value)
        row = rows.fetchone()
        if row is None or tuple(row) != expected:
            raise MigrationError('Prepared collection feedback member differs from its original or reference')
    if rows.fetchone() is not None:
        raise MigrationError('Prepared collection feedback contains extra members')


OWNERS = {
    'album-feedback': DocumentPublication('album_feedback', preparation, preparation.FEEDBACK_JOB,
                                         album_feedback_storage.available, verify_rows),
    'account-auth': DocumentPublication('auth_expired_identities', preparation, preparation.AUTH_JOB,
                                       account_auth_state.available, verify_rows),
}
SOURCES = {owner.source: owner for owner in OWNERS.values()}


def validate_installed(conn):
    for owner in OWNERS.values():
        owner.validate_installed(conn)


def main(argv=None):
    return run(OWNERS, __doc__, argv)


if __name__ == '__main__':
    raise SystemExit(main())
