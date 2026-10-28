"""Persist playlist publication ownership across connections and removal/reappearance."""


def statements():
    yield ('CREATE TABLE playlist_library_revisions ('
           'scope_ref TEXT NOT NULL REFERENCES playlist_scopes(ref),'
           'identity_id INTEGER NOT NULL REFERENCES identities(id),'
           "revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),"
           'PRIMARY KEY(scope_ref,identity_id))')
    # Revision values are opaque change tokens, not counts of user edits. Storage
    # triggers cover ordinary edits, Radio, recovery and compatibility-view writes.
    for table in ('playlists', 'playlist_items', 'unresolved_playlist_items', 'actions'):
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            sides = ('OLD', 'NEW') if event == 'UPDATE' else ('OLD',) if event == 'DELETE' else ('NEW',)
            writes = []
            for side in sides:
                if table == 'playlists':
                    source = f'SELECT {side}.scope_ref,{side}.identity_id,1 WHERE {side}.scope_ref IS NOT NULL'
                else:
                    if table == 'actions':
                        owner = f"json_extract(CASE WHEN json_valid({side}.plan_json) THEN {side}.plan_json END,'$.playlist_id')"
                        predicate = f" AND {side}.kind='add_tracks' AND {side}.status='pending'"
                    else:
                        owner, predicate = f'{side}.playlist_id', ''
                    source = ('SELECT scope_ref,identity_id,1 FROM playlist_membership_refs '
                              f'WHERE id={owner} AND scope_ref IS NOT NULL{predicate}')
                writes.append('INSERT INTO playlist_library_revisions ' + source +
                              ' ON CONFLICT(scope_ref,identity_id) DO UPDATE SET revision=revision+1;')
            yield (f'CREATE TRIGGER playlist_revision_{table}_{event.lower()} AFTER {event} ON {table} '
                   'BEGIN ' + ''.join(writes) + ' END')


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
