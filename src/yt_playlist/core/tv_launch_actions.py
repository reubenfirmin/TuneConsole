"""TV playlist creation and its outcome on the existing action owner."""
import hashlib
import json
from uuid import uuid4

from . import collection_recovery
from .playlist_recipe_state import REFERENCE


KIND = 'tv_launch'
PREFIX = 'tv-launch:'
ACCOUNT_COLUMN = 'tv_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL'
FIELDS = ('title', 'artist', 'thumbnail', 'duration', 'kind', 'source_key',
          'source_artist', 'source_group', 'connection')
OUTCOMES = ('confirmed', 'uncertain', 'rejected', 'unattempted')


def _record(conn, request):
    return conn.execute('SELECT * FROM actions WHERE idempotency_key=? AND kind=?', (PREFIX + request, KIND)).fetchone()


def _document(row):
    reference = tuple(row[field] for field in REFERENCE)
    value = collection_recovery.read(*reference, kind='action', owner=row['id'])
    state = json.loads(value['params'])
    state.update(account_id=row['tv_account_id'], state='complete' if row['status'] == 'executed' else 'pending')
    return state


@collection_recovery.pinned_read
def read(conn, request):
    row = _record(conn, request)
    if row is None:
        return None
    result = _document(row)
    result['items'] = []
    result['outcome'] = {state: [] for state in OUTCOMES}
    for item in result.pop('videos'):
        track = {field: item[field] for field in ('video_id', *FIELDS) if item.get(field) is not None}
        if 'source_group' in track:
            track['group'] = track.pop('source_group')
        result['items'].append(track)
        result['outcome'][item['write_state']].append(item['video_id'])
    return result


def _pending(conn, account):
    return conn.execute("SELECT * FROM actions WHERE kind=? AND status='pending' AND tv_account_id=? ORDER BY id DESC",
                        (KIND, account))


@collection_recovery.pinned_read
def pending(conn, account):
    for row in _pending(conn, account):
        state = _document(row)
        if state['current']:
            return state['request_id']
    return None


def pending_count(conn):
    return conn.execute("SELECT count(*) FROM actions WHERE kind=? AND status='pending'", (KIND,)).fetchone()[0]


@collection_recovery.pinned_read
def save(conn, state, row=None):
    state = dict(state)
    status = 'executed' if state.pop('state') == 'complete' else 'pending'
    account = state.pop('account_id')
    if row is None:
        identity = conn.execute("SELECT max(coalesce((SELECT max(id) FROM actions),0),"
            "coalesce((SELECT max(seq) FROM sqlite_sequence WHERE name IN ('actions','collection_file_writes')),0))+1").fetchone()[0]
    else:
        identity = row['id']
    document = dict(params=json.dumps(state), plan=None, undo=None)
    reference = collection_recovery.publish(conn, 'action', identity, document)
    revision = uuid4().hex
    if row is None:
        key = PREFIX + state['request_id']
        digest = hashlib.sha256(key.encode()).hexdigest()
        conn.execute('INSERT INTO actions(id,kind,status,revision,idempotency_key,intent_digest,created_at,'
                     'tv_account_id,recovery_filename,recovery_digest,recovery_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     (identity, KIND, status, revision, key, digest, state['created_at'], account, *reference))
    else:
        conn.execute('UPDATE actions SET status=?,revision=?,recovery_filename=?,recovery_digest=?,recovery_bytes=? '
                     'WHERE id=?', (status, revision, *reference, identity))


@collection_recovery.pinned_read
def insert(conn, request, account, stream, maximum, items, now, *, current=True):
    if not 0 < len(request) <= 64 or len(stream) != 32 or maximum is not None and maximum <= 0:
        raise ValueError('Invalid TV launch identity or duration')
    if current:
        for row in _pending(conn, account).fetchall():
            state = _document(row)
            if state['current']:
                save(conn, dict(state, current=0), row)
    videos = []
    for item in items:
        video = {field: item.get('group' if field == 'source_group' else field) for field in FIELDS}
        videos.append(dict(video, video_id=item['video_id'], write_state='uncertain'))
    save(conn, dict(request_id=request, account_id=account, owner_id=account, stream=stream,
                    max_minutes=maximum, provider_playlist_id=None, title='TV', state='pending',
                    current=int(current), created_at=now, videos=videos))


@collection_recovery.pinned_read
def destination(conn, request, remote, title):
    row = _record(conn, request)
    if row is None or not remote:
        raise ValueError('The TV destination belongs to another request')
    state = _document(row)
    if state['provider_playlist_id'] not in (None, remote):
        raise ValueError('The TV destination belongs to another request')
    save(conn, dict(state, provider_playlist_id=remote, title=title), row)


@collection_recovery.pinned_read
def outcome(conn, request, values):
    changes = {video: status for status, videos in values.items() for video in videos}
    if not changes or (row := _record(conn, request)) is None:
        return
    if set(changes.values()) - set(OUTCOMES):
        raise ValueError('Invalid TV launch video outcome')
    state = _document(row)
    for item in state['videos']:
        item['write_state'] = changes.get(item['video_id'], item['write_state'])
    save(conn, state, row)


@collection_recovery.pinned_read
def complete(conn, request):
    row = _record(conn, request)
    if row is None:
        return
    state = _document(row)
    if not state['provider_playlist_id']:
        raise ValueError('A TV launch needs a confirmed destination')
    save(conn, dict(state, state='complete'), row)
