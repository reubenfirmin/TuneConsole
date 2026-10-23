"""Catalog identity operations. Callers own transactions and the Store lock.

The integer entry handle preserves old URLs/actions. It resolves to a recording and,
when known, an exact YouTube item. Text keys are opaque compatibility labels, never
evidence that two uploads contain the same recording.
"""
import json
import time

from yt_playlist.util.matching import identity_key, normalize
from yt_playlist.core.catalog_identity import ITEM_FIELDS, RECORDING_FIELDS, unresolved_ref
from yt_playlist.core.catalog_state import rekey_state as rekey_serialized_state
from yt_playlist.core.discovery_sources import prepare_reassociation, finish_reassociation
from yt_playlist.core import metadata, artists, albums
from yt_playlist.core import rating_feedback
from yt_playlist.util.thumbnails import is_placeholder_art


def resolve_entry(conn, entry_id):
    row = conn.execute('SELECT entry_id FROM legacy_track_refs WHERE legacy_id=?', (entry_id,)).fetchone()
    return row[0] if row else entry_id


def resolve_recording(conn, recording_id):
    seen = set()
    while recording_id not in seen:
        seen.add(recording_id)
        row = conn.execute('SELECT recording_id FROM recording_redirects WHERE retired_id=?', (recording_id,)).fetchone()
        if row is None:
            return recording_id
        recording_id = row[0]
    raise ValueError('Cyclic recording redirects')


def resolve_key(conn, key, video_id=None):
    if video_id and video_id.strip():
        row = conn.execute('SELECT r.identity_key FROM youtube_items y JOIN recordings r ON r.id=y.recording_id WHERE y.video_id=?', (video_id,)).fetchone()
        if row:
            return row[0]
        return None
    row = conn.execute('SELECT id,identity_key FROM recordings WHERE identity_key=?', (key,)).fetchone()
    if row:
        rid = resolve_recording(conn,row[0])
        return conn.execute('SELECT identity_key FROM recordings WHERE id=?', (rid,)).fetchone()[0]
    rows = conn.execute('SELECT DISTINCT r.identity_key FROM recording_aliases a JOIN recordings r ON r.id=a.recording_id WHERE a.alias=?', (key,)).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def choose_field(conn, entry_id, field, value, source='user', *, now=None):
    entry_id = resolve_entry(conn,entry_id)
    if field not in (*ITEM_FIELDS,*RECORDING_FIELDS):
        raise ValueError(f'Unknown metadata field: {field}')
    metadata.record_choice(conn,entry_id,field,value,source,now=now)
    if field == 'artist' and value:
        credits = conn.execute('SELECT artist_id FROM track_artists WHERE entry_id=?', (entry_id,)).fetchall()
        if len(credits) == 1:
            conn.execute("INSERT OR IGNORE INTO artist_aliases VALUES (?,?,'user_correction')",
                         (credits[0][0],value))
        elif not credits:
            artists.observe(conn,entry_id,value,source='user_correction',now=now)
    conn.execute('INSERT INTO entry_field_choices VALUES (?,?,?,?) ON CONFLICT(entry_id,field) DO UPDATE SET value_json=excluded.value_json,source=excluded.source', (entry_id,field,json.dumps(value),source))


def update_artwork(conn, entry_id, url, *, source):
    """Explicit choices win; real YouTube art refreshes, and provider art only fills missing covers.

    Missing/placeholder observations never displace useful artwork, including provider recovery.
    This applies to both discovery and library ingestion because artwork belongs to the item.
    """
    if not isinstance(url, str) or not url.strip():
        return
    entry_id = resolve_entry(conn, entry_id)
    if conn.execute("SELECT 1 FROM entry_field_choices WHERE entry_id=? AND field='thumbnail'",
                    (entry_id,)).fetchone():
        return
    row = conn.execute('SELECT thumbnail FROM catalog_tracks WHERE id=?', (entry_id,)).fetchone()
    if row is None:
        return
    current = row['thumbnail']
    useful = current and current.strip() and not is_placeholder_art(current)
    if is_placeholder_art(url) and (source != 'youtube' or current):
        return
    if source != 'youtube' and useful:
        return
    metadata.select(conn,entry_id,'thumbnail',url.strip(),source)


def play_key(conn, key, video_id):
    resolved = resolve_key(conn,key,video_id)
    if resolved:
        return resolved
    if video_id and video_id.strip() and conn.execute('SELECT 1 FROM recordings WHERE identity_key=?', (key,)).fetchone():
        return '@unresolved-video:' + video_id
    return key


def replace_hints(conn, entry_id, fields):
    """Direct findings can replace retained import hints once; selected values stay protected."""
    entry_id = resolve_entry(conn,entry_id)
    row = conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry_id,)).fetchone()
    if row is None:
        return
    for field,value in fields.items():
        if value is None or field not in {*RECORDING_FIELDS,*ITEM_FIELDS}:
            continue
        hints = conn.execute(
            'SELECT h.* FROM catalog_field_hints h JOIN catalog_entries e ON e.id=h.entry_id '
            'WHERE h.active=1 AND h.field=? AND '+
            ('e.recording_id=?' if field in RECORDING_FIELDS else 'e.id=?'),
            (field,row['recording_id'] if field in RECORDING_FIELDS else entry_id)).fetchall()
        if any(json.loads(h['value_json']) == row[field] for h in hints):
            conn.execute(f'UPDATE catalog_tracks SET {field}=? WHERE id=?', (value,entry_id))
            for hint in hints:
                conn.execute('UPDATE catalog_field_hints SET active=0 WHERE entry_id=? AND field=? AND source=?',
                             (hint['entry_id'],field,hint['source']))


def invalidate_derived(conn):
    from yt_playlist.core.first_play import invalidate
    from yt_playlist.rec.invalidation import catalog_changed

    invalidate(conn)
    catalog_changed(conn)


def rekey_state(conn, old_key, new_key, recording_id, *, previous_recording_id=None):
    """Move only established associations; raw observations and unknown aliases stay intact."""
    subject = previous_recording_id or recording_id
    conn.execute('UPDATE album_tracks SET recording_id=? WHERE entry_id IS NULL AND recording_id=?', (recording_id,subject))
    for table in ('play_events','rec_served_impressions','rec_lane_impressions'):
        conn.execute(f'UPDATE {table} SET identity_key=?,recording_id=? WHERE recording_id=?', (new_key,recording_id,subject))
    conn.execute('UPDATE listening_observations SET identity_key=?,recording_id=? WHERE recording_id=?',
                 (new_key,recording_id,subject))
    from .listening_calendar import reconcile_foreign_days
    for row in conn.execute('SELECT identity_id FROM listening_calendars'):
        reconcile_foreign_days(conn,row[0])
    for row in conn.execute('SELECT rowid,* FROM rec_feedback WHERE recording_id=?', (subject,)).fetchall():
        payload = json.dumps(dict(row))
        conn.execute("INSERT OR IGNORE INTO identity_state_archive VALUES ('feedback_reassociation',?,?,?)", (f'{subject}:{row["rowid"]}:{old_key}',payload,'verified association or alias disambiguation; learning is not replayed'))
        conn.execute('DELETE FROM rec_feedback WHERE rowid=?', (row['rowid'],))
        if previous_recording_id is not None and row['kind'] in ('like', 'dislike', 'like_grad'):
            # Association rating feedback and guards have one owner: the projector
            # below reconciles the retained contributions using the pre-merge rows.
            continue
        conn.execute('INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at,target_type,recording_id) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(surface,item_key,scope) DO UPDATE SET kind=excluded.kind,reason=excluded.reason,until=excluded.until,created_at=excluded.created_at,target_type=excluded.target_type,recording_id=excluded.recording_id WHERE excluded.created_at>=COALESCE(rec_feedback.created_at,0)', (row['surface'],new_key,row['kind'],row['reason'],row['scope'],row['until'],row['created_at'],row['target_type'],recording_id))
    for row in conn.execute('SELECT * FROM rec_impressions WHERE item_key=?', (old_key,)).fetchall():
        conn.execute('INSERT INTO rec_impressions VALUES (?,?,?,?) ON CONFLICT(surface,item_key) DO UPDATE SET views=views+excluded.views,last_shown=MAX(COALESCE(last_shown,0),COALESCE(excluded.last_shown,0))', (row['surface'],new_key,row['views'],row['last_shown']))
    conn.execute('DELETE FROM rec_impressions WHERE item_key=?', (old_key,))
    for row in conn.execute('SELECT rowid migration_rowid,keys FROM rec_mood').fetchall():
        try:
            keys = json.loads(row['keys'])
            if isinstance(keys,list):
                conn.execute('UPDATE rec_mood SET keys=? WHERE rowid=?', (json.dumps(list(dict.fromkeys(new_key if k==old_key else k for k in keys))),row['migration_rowid']))
        except (ValueError,TypeError):
            pass
    for table,idcol,column in [('taste_modes','mode_id','rep_keys'),('cluster_canvas','playlist_ytm','state'),('road_trip_drafts','recipe_id','state'),('rec_recipes','playlist_ytm','recipe')]:
        for row in conn.execute(f'SELECT * FROM {table}').fetchall():
            try:
                value = rekey_serialized_state(json.loads(row[column]),{old_key:new_key})
                conn.execute(f'UPDATE {table} SET {column}=? WHERE {idcol}=?', (json.dumps(value),row[idcol]))
            except (ValueError,TypeError):
                pass


def associate_item(conn, video_id, recording_id, *, source, evidence, now):
    """Apply an explicit verified association; ingestion/provider guesses never call this."""
    if not source or not isinstance(evidence,dict) or not evidence:
        raise ValueError('A verified association requires its source and supporting evidence')
    recording_id = resolve_recording(conn,recording_id)
    item = conn.execute('SELECT recording_id FROM youtube_items WHERE video_id=?', (video_id,)).fetchone()
    target = conn.execute('SELECT identity_key FROM recordings WHERE id=?', (recording_id,)).fetchone()
    if item is None or target is None:
        raise ValueError('Unknown item or recording')
    previous = item[0]
    if previous == recording_id:
        return
    payload = json.dumps(evidence,sort_keys=True)
    previous_key = conn.execute('SELECT identity_key FROM recordings WHERE id=?', (previous,)).fetchone()[0]
    rating_keys = {previous_key, target[0]}
    prior_ratings = rating_feedback.aggregate(conn, rating_keys)
    feedback_before = rating_feedback.feedback_rows(conn, rating_keys)
    association_id = conn.execute('INSERT INTO item_recording_associations(video_id,recording_id,previous_recording_id,source,evidence,at) VALUES (?,?,?,?,?,?)', (video_id,recording_id,previous,source,payload,now)).lastrowid
    member, remaining = prepare_reassociation(conn,video_id,previous)
    conn.execute('UPDATE youtube_items SET recording_id=? WHERE video_id=?', (recording_id,video_id))
    conn.execute('UPDATE catalog_entries SET recording_id=? WHERE video_id=?', (recording_id,video_id))
    finish_reassociation(conn,video_id,previous,recording_id,member,remaining,now,association_id=association_id)
    # Release occurrences retain their exact entry; its current recording resolves at read time.
    conn.execute('UPDATE legacy_track_refs SET recording_id=? WHERE video_id=?', (recording_id,video_id))
    # Verified musical identity permits reuse of recording facts, never item/release fields.
    for field in RECORDING_FIELDS:
        conn.execute(f'UPDATE recordings SET {field}=(SELECT {field} FROM recordings WHERE id=?) '
                     f"WHERE id=? AND ({field} IS NULL OR {field}='')", (previous,recording_id))
    # Exact-video observations can follow the corrected association without changing raw evidence.
    conn.execute('UPDATE play_events SET recording_id=?,identity_key=? WHERE video_id=?', (recording_id,target[0],video_id))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='listening_observations'").fetchone():
        from .listening_calendar import reconcile_foreign_days
        for row in conn.execute('SELECT DISTINCT identity_id FROM play_events WHERE video_id=?',(video_id,)).fetchall():
            reconcile_foreign_days(conn,row[0])
    conn.execute('UPDATE recordings SET provisional=0 WHERE id=?', (recording_id,))
    retired = not conn.execute('SELECT 1 FROM catalog_entries WHERE recording_id=?', (previous,)).fetchone()
    if retired:
        rekey_state(conn,previous_key,target[0],recording_id,previous_recording_id=previous)
        conn.execute('INSERT OR REPLACE INTO recording_redirects VALUES (?,?,?,?)', (previous,recording_id,source,payload))
        conn.execute('UPDATE recording_redirects SET recording_id=? WHERE recording_id=?', (recording_id,previous))
        for row in conn.execute('SELECT alias,source FROM recording_aliases WHERE recording_id=?', (previous,)).fetchall():
            conn.execute('INSERT OR IGNORE INTO recording_aliases VALUES (?,?,?)', (row['alias'],recording_id,row['source']))
        conn.execute('DELETE FROM recording_aliases WHERE recording_id=?', (previous,))
    combined = rating_feedback.aggregate(conn, rating_keys)
    # An exact upload's contribution follows its corrected recording, including partial
    # reassociation. Do not leave feedback on the old recording or replay its learning.
    for key in prior_ratings.keys() - combined.keys():
        combined[key] = rating_feedback.Rating('INDIFFERENT', now)
    rating_feedback.apply(conn, combined, prior_feedback={
        previous_key: [] if retired else [row for row in feedback_before if row['item_key'] == previous_key],
        target[0]: feedback_before})
    invalidate_derived(conn)


def add_alias(conn, alias, recording_id, source):
    conn.execute('INSERT OR IGNORE INTO recording_aliases VALUES (?,?,?)', (alias,recording_id,source))
    rows = conn.execute('SELECT r.id,r.identity_key,r.artist FROM recording_aliases a JOIN recordings r ON r.id=a.recording_id WHERE a.alias=?', (alias,)).fetchall()
    if len(rows) < 2:
        return
    # Once an alias becomes ambiguous, nobody keeps the bare key as a privileged first match.
    renamed = False
    for row in rows:
        if row['identity_key'] != alias:
            continue
        key = f"@recording:{row['id']}|" + normalize(row['artist'] or '')
        conn.execute('UPDATE recordings SET identity_key=? WHERE id=?', (key,row['id']))
        rekey_state(conn,alias,key,row['id'])
        renamed = True
    # Existing embeddings/proposals use the previous key space. Rebuild, retaining rollback artifacts.
    if renamed:
        invalidate_derived(conn)


def upsert(conn, video_id, title, artist, album=None, duration_s=None, available=None,
           video_type=None, artist_browse_id=None, album_browse_id=None, thumbnail=None,
           created_at=None, *, library=False, source_ref=None, preferred_key=None, credits=None):
    video_id = video_id if video_id and video_id.strip() else None
    source_ref = None if video_id else (source_ref or unresolved_ref(album_browse_id or album,title,artist))
    row = conn.execute('SELECT * FROM catalog_entries WHERE video_id=?' if video_id else 'SELECT * FROM catalog_entries WHERE source_ref=?', (video_id or source_ref,)).fetchone()
    now = time.time() if created_at is None else created_at
    if row is None:
        alias = preferred_key or identity_key(title,artist)
        rid = conn.execute('INSERT INTO recordings(identity_key,title,artist,created_at) VALUES (?,?,?,?)', ('@new:'+str(time.time_ns()),title,artist,now)).lastrowid
        existing = conn.execute('SELECT 1 FROM recording_aliases WHERE alias=? LIMIT 1', (alias,)).fetchone()
        key = f'@recording:{rid}|' + normalize(artist or '') if existing else alias
        conn.execute('UPDATE recordings SET identity_key=? WHERE id=?', (key,rid))
        if video_id:
            conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist) VALUES (?,?,?,?,?,?)', (video_id,rid,title,artist,title,artist))
        entry_id = conn.execute('INSERT INTO catalog_entries(recording_id,video_id,source_ref) VALUES (?,?,?)', (rid,video_id,source_ref)).lastrowid
        if not video_id:
            conn.execute('INSERT INTO unresolved_items(entry_id,title,artist,orig_title,orig_artist) VALUES (?,?,?,?,?)', (entry_id,title,artist,title,artist))
        add_alias(conn,alias,rid,'youtube' if video_id else 'source_entry')
        add_alias(conn,identity_key(title,artist),rid,'youtube' if video_id else 'source_entry')
        if video_id:
            conn.execute("INSERT INTO item_recording_associations(video_id,recording_id,source,evidence,at) VALUES (?,?,'new_upload','{}',?)", (video_id,rid,now))
            current_key = conn.execute('SELECT identity_key FROM recordings WHERE id=?', (rid,)).fetchone()[0]
            conn.execute('UPDATE play_events SET recording_id=?,identity_key=? WHERE video_id=? AND recording_id IS NULL', (rid,current_key,video_id))
        # An unresolved exact-video observation must never borrow this new recording's text key.
        current_key = conn.execute('SELECT identity_key FROM recordings WHERE id=?', (rid,)).fetchone()[0]
        conn.execute("UPDATE play_events SET identity_key='@unresolved-video:'||video_id WHERE recording_id IS NULL AND identity_key=? AND NULLIF(trim(video_id),'') IS NOT NULL AND NOT EXISTS(SELECT 1 FROM youtube_items y WHERE y.video_id=play_events.video_id)", (current_key,))
    else:
        entry_id, rid = row['id'], row['recording_id']
        add_alias(conn,identity_key(title,artist),rid,'youtube' if video_id else 'source_entry')
    # Metadata refresh never overwrites an effective title/credit correction or its reset value.
    values = dict(album=album,duration_s=duration_s,available=None if available is None else int(available),
                  video_type=video_type,artist_browse_id=artist_browse_id,album_browse_id=album_browse_id)
    for field,value in values.items():
        if value is not None:
            selected = metadata.selected_source(conn,entry_id,field)
            if selected in (None,'youtube','legacy_unknown'):
                metadata.select(conn,entry_id,field,value,'youtube',now=now)
    for field,value in (('title',title),('artist',artist),('orig_title',title),('orig_artist',artist)):
        if metadata.selected_source(conn,entry_id,field) is None:
            metadata.select(conn,entry_id,field,value,'youtube',now=now)
    artists.observe(conn,entry_id,artist,credits,browse_id=artist_browse_id,now=now)
    update_artwork(conn, entry_id, thumbnail, source='youtube')
    if library:
        conn.execute("INSERT OR IGNORE INTO library_memberships VALUES (?,'observed',?)", (entry_id,now))
    if album_browse_id or album:
        albums.link(conn,entry_id,album_browse_id,title=album,artist=artist)
    return entry_id
