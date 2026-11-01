"""Durable draft decisions are independent of candidate recovery files."""
from uuid import uuid4

from . import catalog, collection_recovery
from .road_trip_draft_documents import available, read


def account(conn):
    row = conn.execute('SELECT id FROM accounts WHERE active=1 AND is_master=1').fetchone()
    if row is None:
        raise ValueError('Choose an active account before creating a draft')
    return row[0]


def save(conn, recipe_id, state, now, *, expected_revision=None, conditional=False, legacy=False):
    # Reserve the writer before checking the revision, including across Store
    # instances/processes. The file is durable before its owner is published.
    conn.execute('UPDATE road_trip_drafts SET revision=revision WHERE 0')
    current = conn.execute('SELECT * FROM road_trip_drafts WHERE recipe_id=?',(recipe_id,)).fetchone()
    if conditional and (current is None or current['revision']!=expected_revision):
        return False
    owner = current['id'] if current else uuid4().hex
    account_id = current['account_id'] if current else None if legacy else account(conn)
    revision = state.get('revision')
    if not isinstance(revision,str) or not revision:
        if legacy:
            revision = 'legacy:'+uuid4().hex
            state = {**state,'revision':revision}
        else:
            raise ValueError('A draft requires a nonempty revision')
    recovery = dict(state)
    selections = []
    for selection, values in (('picked',state.get('picks',[])),('excluded',state.get('banned',[]))):
        for position, video in enumerate(values):
            if not isinstance(video,str) or not video:
                raise ValueError('A draft selection requires an exact upload ID')
            row = conn.execute('SELECT id,recording_id FROM catalog_entries WHERE video_id=? ORDER BY id LIMIT 1',(video,)).fetchone()
            if row is None:
                # The selected upload is exact evidence. Admit it through the
                # normal catalog boundary; do not infer a recording from title.
                entry = catalog.upsert(conn,video,'','',preferred_key='@unresolved-video:'+video)
                row = conn.execute('SELECT id,recording_id FROM catalog_entries WHERE id=?',(entry,)).fetchone()
            selections.append((selection,position,row[1],row[0],video))
    recovery.pop('picks',None)
    recovery.pop('banned',None)
    filename,digest,size = collection_recovery.publish(conn,'road-trip-draft',owner,recovery)
    conn.execute('INSERT INTO road_trip_drafts(id,recipe_id,account_id,legacy_author_unknown,revision,state,'
        'recovery_filename,recovery_digest,recovery_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(recipe_id) DO UPDATE SET revision=excluded.revision,state=excluded.state,'
        'recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,'
        'recovery_bytes=excluded.recovery_bytes,updated_at=excluded.updated_at',
        (owner,recipe_id,account_id,int(account_id is None),revision,'building' if state.get('building') else 'ready',
         filename,digest,size,now))
    conn.execute('DELETE FROM road_trip_draft_entries WHERE draft_id=?',(owner,))
    conn.executemany('INSERT INTO road_trip_draft_entries(draft_id,selection_state,position,recording_id,entry_id,video_id) '
                     'VALUES (?,?,?,?,?,?)',[(owner,*row) for row in selections])
    return True


@collection_recovery.pinned_read
def get(conn,recipe_id=None,*,latest=False):
    row = conn.execute('SELECT * FROM road_trip_drafts ORDER BY updated_at DESC,id DESC LIMIT 1' if latest else
                       'SELECT * FROM road_trip_drafts WHERE recipe_id=?',() if latest else (recipe_id,)).fetchone()
    if row is None:
        return None
    state = read(conn,row)
    return {'recipe_id':row['recipe_id'],'state':state} if latest else state
