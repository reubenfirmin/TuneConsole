"""Verified playback on response receipts and the bounded publication handoff."""
import json
import math
import sqlite3

from . import playlist_scopes, worker_settings


KEY = 'choice_playback_pending'
ORDER = ('account_id', 'playlist_scope_id', 'video_id')
SELECTED = "kind='listening-evidence' AND selected=1 AND playback_account_id IS NOT NULL"
COORDINATES = ('playback_account_id', 'playback_scope_id', 'playback_video_id', 'playback_at')


def save(conn, rows):
    worker_settings.save(conn, KEY, json.dumps(rows, separators=(',', ':')) if rows else None)


def coordinates(record):
    details = record.get('details') or {}
    account, playlist, video, at = (details.get('account_id'), details.get('playlist'),
                                    details.get('video_id'), record.get('at'))
    if (record.get('kind') in {'start', 'experience'} and not details.get('kind')
            and type(account) is int and isinstance(playlist, str) and playlist
            and isinstance(video, str) and video and type(at) in (int, float) and math.isfinite(at)):
        return account, playlist, video, at
    return None


def attach(conn, identity, record):
    account, playlist, video, at = coordinates(record)
    if not conn.execute('SELECT 1 FROM accounts WHERE id=?', (account,)).fetchone():
        return
    scope = playlist_scopes.ensure(conn, playlist, account)
    changed = conn.execute(f"""UPDATE collection_file_writes SET ({','.join(COORDINATES)})=(?,?,?,?)
        WHERE kind='listening-evidence' AND selected=1 AND file_address=? AND owner_id->>0='response'""",
        (account, scope, video, at, identity)).rowcount
    if changed != 1:
        return
    pending = json.loads(worker_settings.read(conn, KEY, '[]'))
    remaining = [row for row in pending if (
        row['account_id'], row['playlist_scope_id'], row['video_id'], row['bound_at'],
        row['opportunity_id'], row['option_id']
    ) != (account, scope, video, at, record['opportunity_id'], record['option_id'])]
    if remaining != pending:
        save(conn, remaining)


def insert(conn, account_id, playlist, video, opportunity, option, now, *, before, limit, response_id=None):
    scope = playlist_scopes.ensure(conn, playlist, account_id)
    owner = conn.execute("SELECT 1 FROM accounts a JOIN playlist_scopes s ON s.id=? "
        "JOIN track_sources y ON y.video_id=? WHERE a.id=? AND (s.kind='shared' OR s.account_id=a.id)",
        (scope, video, account_id)).fetchone()
    if owner is None or opportunity is None or option is None or not math.isfinite(now):
        raise sqlite3.IntegrityError('Playback binding requires an account, playlist scope, upload and choice')
    if response_id is not None and conn.execute(f"""SELECT 1 FROM collection_file_writes WHERE {SELECTED}
        AND owner_id=json_array('response',?) AND playback_account_id=? AND playback_scope_id=?
        AND playback_video_id=? AND playback_at=?""", (response_id, account_id, scope, video, now)).fetchone():
        return
    identity = (account_id, scope, video)
    rows = [row for row in json.loads(worker_settings.read(conn, KEY, '[]'))
            if before <= row['bound_at'] <= now and tuple(row[key] for key in ORDER) != identity]
    rows.append(dict(account_id=account_id, playlist_scope_id=scope, video_id=video,
                     opportunity_id=str(opportunity), option_id=str(option), bound_at=now))
    rows.sort(key=lambda row: (-row['bound_at'], *(row[key] for key in ORDER)))
    save(conn, rows[:max(0, limit)])


def lookup(conn, account_id, playlist, video, *, before, through):
    scope = playlist_scopes.resolve(conn,playlist,account_id) if playlist is not None else None
    if scope is None and not video:
        return None
    args = {'account': account_id, 'scope': scope, 'video': video, 'before': before, 'through': through}
    scope_filter = 'AND playback_scope_id=:scope' if scope is not None else ''
    video_filter = 'AND playback_video_id=:video' if video is not None else ''
    verified = conn.execute(f'''SELECT playback_account_id AS account_id,s.provider_playlist_id AS playlist,
            y.video_id AS video,f.owner_id->>1 AS response_id,playback_at AS at
        FROM collection_file_writes f JOIN playlist_scopes s ON s.id=f.playback_scope_id
        JOIN track_sources y ON y.video_id=f.playback_video_id
        WHERE f.kind='listening-evidence' AND f.selected=1 AND playback_account_id=:account
          AND playback_at BETWEEN :before AND :through
          {scope_filter} {video_filter}
        ORDER BY at DESC,y.video_id DESC,s.id,f.id DESC LIMIT 1''', args).fetchone()
    raw = worker_settings.read(conn, KEY)
    if raw is None:
        return dict(verified) if verified else None
    pending = conn.execute('''SELECT b.value->>'account_id' AS account_id,s.provider_playlist_id AS playlist,
            y.video_id AS video,b.value->>'opportunity_id' AS opportunity_id,b.value->>'option_id' AS option_id,
            b.value->>'bound_at' AS at
        FROM json_each(:bindings) b JOIN accounts a ON a.id=b.value->>'account_id'
        JOIN playlist_scopes s ON s.id=b.value->>'playlist_scope_id' AND (s.kind='shared' OR s.account_id=a.id)
        JOIN track_sources y ON y.video_id=b.value->>'video_id'
        WHERE a.id=:account AND (:scope IS NULL OR s.id=:scope) AND (:video IS NULL OR y.video_id=:video)
            AND at BETWEEN :before AND :through
        ORDER BY at DESC,y.video_id DESC,s.id LIMIT 1''',
        {**args, 'bindings': raw}).fetchone()
    if pending and (verified is None or pending['at'] > verified['at']):
        return dict(pending)
    return dict(verified) if verified else None
