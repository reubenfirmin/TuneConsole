"""Recording catalog shared by library/discovery; uploads remain exact playback targets."""
import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from functools import lru_cache

from .runner import MigrationError

RECORDING_FIELDS = {
    'genre': 'TEXT', 'bpm': 'REAL', 'energy': 'REAL', 'danceability': 'REAL',
    'mb_recording_id': 'TEXT', 'music_key': 'TEXT', 'music_scale': 'TEXT',
    'mood_happy': 'REAL', 'mood_sad': 'REAL', 'mood_relaxed': 'REAL',
    'mood_acoustic': 'REAL', 'instrumental': 'REAL',
}
ITEM_FIELDS = {
    'title': 'TEXT', 'artist': 'TEXT', 'orig_title': 'TEXT', 'orig_artist': 'TEXT',
    'album': 'TEXT', 'duration_s': 'INTEGER', 'available': 'INTEGER', 'video_type': 'TEXT',
    'artist_browse_id': 'TEXT', 'album_browse_id': 'TEXT', 'thumbnail': 'TEXT',
    'mb_year': 'TEXT', 'loudness': 'REAL', 'dynamic_complexity': 'REAL',
    'popularity': 'INTEGER', 'gain': 'REAL', 'label': 'TEXT',
}
REPLACED = {'tracks', 'discovered_tracks', 'playlist_tracks', 'uq_tracks_null_vid'}


def unresolved_ref(album, title, artist):
    # Exact, version-preserving source entry signature; never a cross-release musical identity.
    raw = json.dumps([album or '', title or '', artist or ''], ensure_ascii=False)
    return 'unresolved:' + hashlib.sha256(raw.encode()).hexdigest()


def create_tables(conn):
    fields = ','.join(f'{k} {v}' for k,v in RECORDING_FIELDS.items())
    item_fields = ','.join(f'{k} {v}' for k,v in ITEM_FIELDS.items())
    sqls = [
        f'CREATE TABLE recordings (id INTEGER PRIMARY KEY AUTOINCREMENT,identity_key TEXT NOT NULL UNIQUE,title TEXT,artist TEXT,provisional INTEGER NOT NULL DEFAULT 1,{fields},created_at REAL,first_enriched_at REAL,last_enriched_at REAL)',
        f"CREATE TABLE youtube_items (video_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(video_id))>0),recording_id INTEGER NOT NULL REFERENCES recordings(id),{item_fields},UNIQUE(video_id,recording_id))",
        'CREATE INDEX ix_youtube_recording ON youtube_items(recording_id)',
        'CREATE TABLE catalog_entries (id INTEGER PRIMARY KEY AUTOINCREMENT,recording_id INTEGER NOT NULL REFERENCES recordings(id),video_id TEXT UNIQUE REFERENCES youtube_items(video_id),source_ref TEXT UNIQUE,CHECK((video_id IS NOT NULL)<>(source_ref IS NOT NULL)),FOREIGN KEY(video_id,recording_id) REFERENCES youtube_items(video_id,recording_id) DEFERRABLE INITIALLY DEFERRED)',
        'CREATE INDEX ix_entries_recording ON catalog_entries(recording_id)',
        f'CREATE TABLE unresolved_items (entry_id INTEGER PRIMARY KEY REFERENCES catalog_entries(id),{item_fields})',
        "CREATE TABLE library_memberships (entry_id INTEGER PRIMARY KEY REFERENCES catalog_entries(id),origin TEXT NOT NULL,added_at REAL)",
        'CREATE TABLE recording_aliases (alias TEXT NOT NULL,recording_id INTEGER NOT NULL REFERENCES recordings(id),source TEXT NOT NULL,PRIMARY KEY(alias,recording_id))',
        'CREATE TABLE legacy_track_refs (legacy_id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),recording_id INTEGER NOT NULL REFERENCES recordings(id),video_id TEXT REFERENCES youtube_items(video_id))',
        'CREATE TABLE recording_redirects (retired_id INTEGER PRIMARY KEY REFERENCES recordings(id),recording_id INTEGER NOT NULL REFERENCES recordings(id),source TEXT NOT NULL,evidence TEXT NOT NULL)',
        'CREATE TABLE entry_field_choices (entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),field TEXT NOT NULL,value_json TEXT NOT NULL,source TEXT NOT NULL,PRIMARY KEY(entry_id,field))',
        'CREATE TABLE catalog_field_hints (entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),field TEXT NOT NULL,source TEXT NOT NULL,value_json TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(entry_id,field,source))',
        'CREATE TABLE legacy_track_evidence (kind TEXT NOT NULL,source_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,source_id))',
        "CREATE TABLE recording_identifiers (id INTEGER PRIMARY KEY,recording_id INTEGER NOT NULL REFERENCES recordings(id),scheme TEXT NOT NULL,entity_type TEXT NOT NULL,value TEXT NOT NULL,source TEXT NOT NULL,input_video_id TEXT REFERENCES youtube_items(video_id),status TEXT NOT NULL CHECK(status IN ('candidate','accepted','conflicting')),observed_at REAL)",
        'CREATE INDEX ix_recording_identifier ON recording_identifiers(scheme,entity_type,value)',
        'CREATE TABLE item_recording_associations (id INTEGER PRIMARY KEY,video_id TEXT NOT NULL REFERENCES youtube_items(video_id),recording_id INTEGER NOT NULL REFERENCES recordings(id),previous_recording_id INTEGER REFERENCES recordings(id),source TEXT NOT NULL,evidence TEXT NOT NULL,at REAL)',
        'CREATE TABLE album_track_links (id INTEGER PRIMARY KEY,browse_id TEXT NOT NULL,recording_id INTEGER NOT NULL REFERENCES recordings(id),entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),video_id TEXT REFERENCES youtube_items(video_id),source TEXT NOT NULL,UNIQUE(browse_id,entry_id))',
        'CREATE INDEX ix_album_recording ON album_track_links(recording_id)',
        'CREATE TABLE discovery_memberships (recording_id INTEGER PRIMARY KEY REFERENCES recordings(id),entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),found_at REAL,first_shown REAL,last_shown REAL,offered_count INTEGER NOT NULL DEFAULT 0)',
        'CREATE TABLE discovery_sources (recording_id INTEGER NOT NULL REFERENCES recordings(id),source_token TEXT NOT NULL,input_video_id TEXT REFERENCES youtube_items(video_id),found_at REAL,PRIMARY KEY(recording_id,source_token))',
        'CREATE TABLE playlist_items (playlist_id INTEGER NOT NULL REFERENCES playlists(id),video_id TEXT NOT NULL REFERENCES youtube_items(video_id),position INTEGER NOT NULL,PRIMARY KEY(playlist_id,video_id))',
        'CREATE INDEX ix_playlist_item_video ON playlist_items(video_id)',
        'CREATE TABLE unresolved_playlist_items (playlist_id INTEGER NOT NULL REFERENCES playlists(id),entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),position INTEGER NOT NULL,PRIMARY KEY(playlist_id,entry_id))',
        'CREATE TABLE provider_attempts (id INTEGER PRIMARY KEY,recording_id INTEGER NOT NULL REFERENCES recordings(id),input_video_id TEXT NOT NULL DEFAULT \'\',provider TEXT NOT NULL,at REAL,status TEXT NOT NULL,fields_json TEXT NOT NULL)',
        'CREATE INDEX ix_provider_attempt_subject ON provider_attempts(recording_id,input_video_id,provider,at)',
        'CREATE TABLE legacy_enrichment_attempts (identity_key TEXT PRIMARY KEY,payload TEXT NOT NULL,reason TEXT NOT NULL)',
        'CREATE TABLE identity_state_archive (kind TEXT NOT NULL,source_id TEXT NOT NULL,payload TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(kind,source_id))',
        'CREATE TABLE item_ratings (identity_id INTEGER NOT NULL REFERENCES identities(id),video_id TEXT NOT NULL,status TEXT NOT NULL,source TEXT NOT NULL,observed_at REAL NOT NULL,PRIMARY KEY(identity_id,video_id))',
    ]
    for sql in sqls:
        conn.execute(sql)


def create_views(conn):
    columns = ['e.id', 'e.recording_id', 'e.video_id', 'r.identity_key']
    for c in (*ITEM_FIELDS,*RECORDING_FIELDS):
        value = f'COALESCE(y.{c},u.{c})' if c in ITEM_FIELDS else f'r.{c}'
        if c == 'available':
            value = f'CASE WHEN e.video_id IS NULL THEN 0 ELSE {value} END'
        columns.append(f"CASE WHEN EXISTS(SELECT 1 FROM entry_field_choices ch WHERE ch.entry_id=e.id AND ch.field='{c}') THEN (SELECT json_extract(ch.value_json,'$') FROM entry_field_choices ch WHERE ch.entry_id=e.id AND ch.field='{c}') ELSE {value} END AS {c}")
    columns += ['r.created_at','r.first_enriched_at','r.last_enriched_at']
    conn.execute('CREATE VIEW catalog_tracks AS SELECT '+','.join(columns)+' FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id LEFT JOIN youtube_items y ON y.video_id=e.video_id LEFT JOIN unresolved_items u ON u.entry_id=e.id')
    conn.execute('CREATE VIEW tracks AS SELECT c.* FROM catalog_tracks c JOIN library_memberships m ON m.entry_id=c.id')
    conn.execute('CREATE VIEW playable_library_tracks AS SELECT t.* FROM tracks t JOIN (SELECT e.recording_id,MIN(e.id) entry_id FROM catalog_entries e JOIN library_memberships m ON m.entry_id=e.id JOIN youtube_items y ON y.video_id=e.video_id WHERE COALESCE(y.available,1)<>0 GROUP BY e.recording_id) playable ON playable.entry_id=t.id')
    conn.execute('CREATE VIEW playlist_tracks AS SELECT p.playlist_id,e.id track_id,p.position FROM playlist_items p JOIN catalog_entries e ON e.video_id=p.video_id UNION ALL SELECT playlist_id,entry_id,position FROM unresolved_playlist_items')
    conn.execute("CREATE TRIGGER playlist_tracks_insert INSTEAD OF INSERT ON playlist_tracks BEGIN INSERT INTO playlist_items SELECT NEW.playlist_id,video_id,NEW.position FROM catalog_entries WHERE id=COALESCE((SELECT entry_id FROM legacy_track_refs WHERE legacy_id=NEW.track_id),NEW.track_id) AND video_id IS NOT NULL; INSERT INTO unresolved_playlist_items SELECT NEW.playlist_id,id,NEW.position FROM catalog_entries WHERE id=COALESCE((SELECT entry_id FROM legacy_track_refs WHERE legacy_id=NEW.track_id),NEW.track_id) AND video_id IS NULL; SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM catalog_entries WHERE id=COALESCE((SELECT entry_id FROM legacy_track_refs WHERE legacy_id=NEW.track_id),NEW.track_id)) THEN RAISE(ABORT,'Unknown track reference') END; END")
    conn.execute('CREATE TRIGGER playlist_tracks_delete INSTEAD OF DELETE ON playlist_tracks BEGIN DELETE FROM playlist_items WHERE playlist_id=OLD.playlist_id AND video_id=(SELECT video_id FROM catalog_entries WHERE id=OLD.track_id); DELETE FROM unresolved_playlist_items WHERE playlist_id=OLD.playlist_id AND entry_id=OLD.track_id; END')
    # Compatibility projections contain no duplicate metadata storage. Updates have explicit scope.
    def changed(columns):
        return ','.join(f'{c}=CASE WHEN NEW.{c} IS NOT OLD.{c} THEN NEW.{c} ELSE {c} END' for c in columns)
    shared = changed((*RECORDING_FIELDS,'first_enriched_at','last_enriched_at','created_at'))
    item = changed(ITEM_FIELDS)
    for view in ('catalog_tracks','tracks'):
        conn.execute(f'CREATE TRIGGER {view}_update INSTEAD OF UPDATE ON {view} BEGIN UPDATE recordings SET {shared} WHERE id=OLD.recording_id; UPDATE youtube_items SET {item} WHERE video_id=OLD.video_id; UPDATE unresolved_items SET {item} WHERE entry_id=OLD.id; END')
    conn.execute("CREATE VIEW discovered_tracks AS SELECT c.*,c.mb_year year,d.found_at,d.first_shown,d.last_shown,d.offered_count,c.last_enriched_at last_enriched,(SELECT source_token FROM discovery_sources s WHERE s.recording_id=d.recording_id ORDER BY s.found_at,source_token LIMIT 1) source_browse_id FROM discovery_memberships d JOIN catalog_tracks c ON c.id=d.entry_id")
    conn.execute('CREATE TRIGGER discovered_tracks_delete INSTEAD OF DELETE ON discovered_tracks BEGIN DELETE FROM discovery_sources WHERE recording_id=OLD.recording_id; DELETE FROM discovery_memberships WHERE recording_id=OLD.recording_id; END')
    audio = ','.join(f'{c}=NEW.{c}' for c in RECORDING_FIELDS)
    conn.execute(f'CREATE TRIGGER discovered_tracks_update INSTEAD OF UPDATE ON discovered_tracks BEGIN UPDATE catalog_tracks SET {audio},mb_year=NEW.year,first_enriched_at=COALESCE(first_enriched_at,NEW.last_enriched),last_enriched_at=NEW.last_enriched WHERE id=OLD.id; UPDATE discovery_memberships SET first_shown=NEW.first_shown,last_shown=NEW.last_shown,offered_count=NEW.offered_count WHERE recording_id=OLD.recording_id; END')
    conn.execute("CREATE VIEW catalog_enrichment_tracks AS SELECT c.*,r.id queue_order,CASE WHEN EXISTS(SELECT 1 FROM library_memberships lm JOIN catalog_entries e ON e.id=lm.entry_id WHERE e.recording_id=r.id) THEN 'library' ELSE 'discovery' END source FROM recordings r JOIN catalog_tracks c ON c.id=COALESCE((SELECT MIN(e.id) FROM catalog_entries e JOIN library_memberships lm ON lm.entry_id=e.id WHERE e.recording_id=r.id),(SELECT entry_id FROM discovery_memberships WHERE recording_id=r.id))")
    # Raw keys and videos remain in the ledgers. Associations add no event rows and never guess
    # between recordings sharing a text alias. Unknown nonempty videos do not fall back to text.
    for table in ('play_events','history_items','rec_served_impressions','rec_lane_impressions'):
        lookup = "COALESCE((SELECT id FROM recordings WHERE identity_key=NEW.identity_key),(SELECT MIN(recording_id) FROM recording_aliases WHERE alias=NEW.identity_key HAVING COUNT(DISTINCT recording_id)=1))"
        lookup = f'COALESCE((SELECT recording_id FROM recording_redirects WHERE retired_id={lookup}),{lookup})'
        if table == 'play_events':
            lookup = f"CASE WHEN NEW.video_id IS NOT NULL THEN (SELECT recording_id FROM youtube_items WHERE video_id=NEW.video_id) ELSE {lookup} END"
        conn.execute(f'CREATE TRIGGER {table}_subject AFTER INSERT ON {table} BEGIN UPDATE {table} SET raw_identity_key=NEW.identity_key,recording_id={lookup} WHERE rowid=NEW.rowid; UPDATE {table} SET identity_key=COALESCE((SELECT identity_key FROM recordings WHERE id={table}.recording_id),identity_key) WHERE rowid=NEW.rowid; END')
    conn.execute("CREATE TRIGGER feedback_subject AFTER INSERT ON rec_feedback BEGIN UPDATE rec_feedback SET recording_id=(SELECT id FROM recordings WHERE identity_key=NEW.item_key),target_type=CASE WHEN EXISTS(SELECT 1 FROM recordings WHERE identity_key=NEW.item_key) THEN 'recording' ELSE target_type END WHERE rowid=NEW.rowid; END")


def _archive_value(value):
    if isinstance(value, bytes):
        return {'sqlite_blob_hex': value.hex()}
    raise TypeError(f'Unsupported archived SQLite value: {type(value).__name__}')


def _archive(conn, kind, source_id, row, reason='legacy identity conversion'):
    conn.execute('INSERT OR IGNORE INTO identity_state_archive VALUES (?,?,?,?)', (kind,str(source_id),json.dumps(dict(row),default=_archive_value),reason))


def _key(recording_id, artist):
    from yt_playlist.util.matching import normalize
    return f'@recording:{recording_id}|' + normalize(artist or '')


def apply(conn, context):
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        _apply(conn, context)
    finally:
        conn.row_factory = previous_factory


def _apply(conn, context):
    old = [dict(r) for r in conn.execute('SELECT * FROM tracks ORDER BY id')]
    discovered = [dict(r) for r in conn.execute('SELECT rowid legacy_rowid,* FROM discovered_tracks')]
    playlists = [tuple(r) for r in conn.execute('SELECT playlist_id,track_id,position FROM playlist_tracks ORDER BY playlist_id,position,rowid')]
    create_tables(conn)
    groups, old_map, video_map, alias_ids = {}, {}, {}, defaultdict(set)
    maximum = max((r['id'] for r in old), default=0)
    next_id = maximum + 1
    for row in old:
        video = row['video_id'] if row['video_id'] and row['video_id'].strip() else None
        group = ('video',video) if video else ('legacy',row['id'])
        groups.setdefault(group, []).append(('library',row))
    for row in discovered:
        row = {**row,'mb_year':row['year'],'created_at':row['found_at'],
               'first_enriched_at':row['last_enriched'],'last_enriched_at':row['last_enriched']}
        video = row['video_id'] if row['video_id'] and row['video_id'].strip() else None
        group = ('video',video) if video else ('discovery',row['identity_key'])
        groups.setdefault(group, []).append(('discovery',row))
    for group, rows in groups.items():
        library = [r for kind,r in rows if kind == 'library']
        rid = min(r['id'] for r in library) if library else next_id
        if not library:
            next_id += 1
        video = group[1] if group[0] == 'video' else None
        # Survivor identity does not choose metadata precedence. Preserve every original row too.
        effective = dict(rows[0][1])
        for field in (*RECORDING_FIELDS,*ITEM_FIELDS,'created_at','first_enriched_at','last_enriched_at'):
            candidates = [r for _,r in rows if r.get(field) is not None]
            if field in ('title','artist'):
                edited = [r for r in candidates if r.get('orig_'+field) is not None and r[field] != r['orig_'+field]]
                candidates = edited or candidates
            if candidates:
                effective[field] = candidates[0][field]
        for field in ('title','artist'):
            chosen = next((r for _,r in rows if r.get(field) == effective.get(field)), effective)
            effective['orig_'+field] = chosen.get('orig_'+field, chosen.get(field))
        key = f'@pending:{rid}'
        fields = ['id','identity_key','title','artist',*RECORDING_FIELDS,'created_at','first_enriched_at','last_enriched_at']
        values = [rid,key,effective.get('title'),effective.get('artist'),*[effective.get(c) for c in RECORDING_FIELDS],effective.get('created_at'),effective.get('first_enriched_at'),effective.get('last_enriched_at')]
        conn.execute(f"INSERT INTO recordings({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values)
        if video:
            cols = ['video_id','recording_id',*ITEM_FIELDS]
            conn.execute(f"INSERT INTO youtube_items({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", [video,rid,*[effective.get(c) for c in ITEM_FIELDS]])
            video_map[video] = rid
        ref = unresolved_ref(effective.get('album_browse_id') or effective.get('source_browse_id') or effective.get('album'),effective.get('title'),effective.get('artist')) if not video else None
        if ref and conn.execute('SELECT 1 FROM catalog_entries WHERE source_ref=?', (ref,)).fetchone():
            ref += f':legacy:{rid}'
        conn.execute('INSERT INTO catalog_entries VALUES (?,?,?,?)', (rid,rid,video,ref))
        if not video:
            cols = ['entry_id',*ITEM_FIELDS]
            conn.execute(f"INSERT INTO unresolved_items({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", [rid,*[effective.get(c) for c in ITEM_FIELDS]])
        if library:
            conn.execute("INSERT INTO library_memberships VALUES (?,'legacy',?)", (rid,effective.get('created_at')))
        for kind,row in rows:
            alias_ids[row['identity_key']].add(rid)
            conn.execute('INSERT OR IGNORE INTO recording_aliases VALUES (?,?,?)', (row['identity_key'],rid,'legacy_'+kind))
            source_id = str(row['id']) if kind == 'library' else row['identity_key']
            conn.execute('INSERT INTO legacy_track_evidence VALUES (?,?,?)', (kind,source_id,json.dumps(row)))
            if kind == 'library':
                old_map[row['id']] = (rid,video)
                conn.execute('INSERT INTO legacy_track_refs VALUES (?,?,?,?)', (row['id'],rid,rid,video))
            else:
                conn.execute('INSERT INTO discovery_memberships VALUES (?,?,?,?,?,?) ON CONFLICT(recording_id) DO NOTHING', (rid,rid,row['found_at'],row.get('first_shown'),row.get('last_shown'),row.get('offered_count') or 0))
                if row.get('source_browse_id'):
                    conn.execute('INSERT OR IGNORE INTO discovery_sources VALUES (?,?,?,?)', (rid,row['source_browse_id'],video,row['found_at']))
                    if conn.execute('SELECT 1 FROM discovered_albums WHERE browse_id=?', (row['source_browse_id'],)).fetchone():
                        conn.execute("INSERT OR IGNORE INTO album_track_links(browse_id,recording_id,entry_id,video_id,source) VALUES (?,?,?,?,'legacy_discovery')", (row['source_browse_id'],rid,rid,video))
            album = row.get('album_browse_id')
            if album:
                conn.execute('INSERT OR IGNORE INTO album_track_links(browse_id,recording_id,entry_id,video_id,source) VALUES (?,?,?,?,?)', (album,rid,rid,video,'legacy'))
            if row.get('mb_recording_id'):
                conn.execute("INSERT INTO recording_identifiers(recording_id,scheme,entity_type,value,source,input_video_id,status,observed_at) VALUES (?,'musicbrainz','recording',?,'legacy_match',?,'candidate',?)", (rid,row['mb_recording_id'],video,context.now))
            if video:
                conn.execute("INSERT INTO item_recording_associations(video_id,recording_id,source,evidence,at) VALUES (?,?,'legacy_exact_video',?,?)", (video,rid,json.dumps({'kind':kind,'source_id':source_id}),context.now))
        for field in ('title','artist'):
            if effective.get('orig_'+field) is not None and effective.get(field) != effective['orig_'+field]:
                conn.execute("INSERT INTO entry_field_choices VALUES (?,?,?,'legacy_edit')", (rid,field,json.dumps(effective.get(field))))
    keymap, recording_keys = {}, {}
    for row in conn.execute('SELECT id,artist FROM recordings').fetchall():
        rid = row['id']
        aliases = [alias for alias, ids in alias_ids.items() if rid in ids and len(ids) == 1]
        key = sorted(aliases)[0] if aliases else _key(rid,row['artist'])
        recording_keys[rid] = key
        conn.execute('UPDATE recordings SET identity_key=? WHERE id=?', (key,rid))
    for alias, ids in alias_ids.items():
        if len(ids) == 1:
            keymap[alias] = recording_keys[next(iter(ids))]
    for pid, old_id, position in playlists:
        rid, video = old_map[old_id]
        if video:
            conn.execute('INSERT OR IGNORE INTO playlist_items VALUES (?,?,?)', (pid,video,position))
        else:
            conn.execute('INSERT OR IGNORE INTO unresolved_playlist_items VALUES (?,?,?)', (pid,rid,position))
    for pid in {row[0] for row in playlists}:
        original = conn.execute('SELECT * FROM playlists WHERE id=?', (pid,)).fetchone()
        _archive(conn,'playlists',pid,original,'membership deduplicated by exact upload; count/hash reconciled')
        subjects = conn.execute('SELECT e.recording_id FROM playlist_items p JOIN catalog_entries e ON e.video_id=p.video_id WHERE p.playlist_id=? UNION ALL SELECT e.recording_id FROM unresolved_playlist_items p JOIN catalog_entries e ON e.id=p.entry_id WHERE p.playlist_id=?', (pid,pid)).fetchall()
        digest = hashlib.sha256('\n'.join(sorted(recording_keys[r[0]] for r in subjects)).encode()).hexdigest()
        conn.execute('UPDATE playlists SET track_count=?,content_hash=? WHERE id=?', (len(subjects),digest,pid))
    for table in ('enrichment_log','enrichment_conflict'):
        conn.execute(f'ALTER TABLE {table} ADD COLUMN recording_id INTEGER REFERENCES recordings(id)')
        conn.execute(f'ALTER TABLE {table} ADD COLUMN input_video_id TEXT')
        conn.execute(f"ALTER TABLE {table} ADD COLUMN field_scope TEXT NOT NULL DEFAULT 'legacy_item'")
        for old_id, (rid,video) in old_map.items():
            conn.execute(f'UPDATE {table} SET recording_id=?,input_video_id=? WHERE track_id=?', (rid,video,old_id))
    # Resolved values are demonstrable choices, including deliberate blanks. Retain scope.
    for row in conn.execute('SELECT * FROM enrichment_conflict WHERE resolved=1'):
        mapped = old_map.get(row['track_id'])
        if mapped:
            field = 'mb_year' if row['field'] == 'year' else row['field']
            if field in ITEM_FIELDS or field in RECORDING_FIELDS:
                value = row['resolved_value']
                datatype = ITEM_FIELDS.get(field, RECORDING_FIELDS.get(field))
                if datatype in ('REAL','INTEGER'):
                    try:
                        value = (float if datatype == 'REAL' else int)(value) if value not in (None,'') else None
                    except (ValueError,TypeError):
                        pass
                conn.execute("INSERT OR IGNORE INTO entry_field_choices VALUES (?,?,?,'legacy_resolution')", (mapped[0],field,json.dumps(value)))
    conflicts = defaultdict(list)
    for row in conn.execute('SELECT * FROM enrichment_conflict'):
        _archive(conn,'enrichment_conflict',row['id'],row)
        target = old_map.get(row['track_id'],(row['track_id'],None))[0]
        conflicts[(target,row['field'])].append(dict(row))
    conn.execute('DELETE FROM enrichment_conflict')
    for (entry_id,field), rows in conflicts.items():
        candidates, seen = [], set()
        for row in rows:
            for candidate in json.loads(row['candidates']):
                signature = json.dumps(candidate,sort_keys=True)
                if signature not in seen:
                    candidates.append(candidate)
                    seen.add(signature)
        selected = next((r for r in rows if r['resolved']),rows[0])
        resolved = int(all(r['resolved'] for r in rows) and len({r['resolved_value'] for r in rows}) == 1)
        conn.execute('INSERT INTO enrichment_conflict(track_id,field,candidates,resolved,resolved_value,updated_at,recording_id,input_video_id,field_scope) VALUES (?,?,?,?,?,?,?,?,?)', (entry_id,field,json.dumps(candidates),resolved,selected['resolved_value'],selected['updated_at'],selected['recording_id'],selected['input_video_id'],'legacy_item'))
    remap_state(conn, keymap, alias_ids, recording_keys, video_map)
    # Preserve attempts whose text identity cannot establish a unique recording.
    for row in conn.execute("SELECT key,value FROM settings WHERE key LIKE 'enrich_attempts:%'").fetchall():
        alias, payload = row['key'][len('enrich_attempts:'):], row['value']
        ids = alias_ids.get(alias,set())
        try:
            attempts = json.loads(payload)
            if len(ids) != 1 or not isinstance(attempts,dict):
                raise ValueError('ambiguous identity')
            rid = next(iter(ids))
            for provider, result in attempts.items():
                conn.execute('INSERT INTO provider_attempts(recording_id,input_video_id,provider,at,status,fields_json) VALUES (?,?,?,?,?,?)', (rid,'',provider,result.get('at'),result.get('status','retry'),json.dumps(result.get('fields',{}))))
                if result.get('status') == 'retry':
                    conn.execute('UPDATE recordings SET first_enriched_at=NULL,last_enriched_at=NULL WHERE id=?', (rid,))
        except (ValueError,TypeError,AttributeError):
            conn.execute('INSERT INTO legacy_enrichment_attempts VALUES (?,?,?)', (alias,payload or '', 'unresolved legacy identity or format'))
        conn.execute('DELETE FROM settings WHERE key=?', (row['key'],))
    # Reserve even retired legacy numeric IDs against reuse.
    for table in ('recordings','catalog_entries'):
        conn.execute('UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?', (max(maximum,next_id-1),table))
    conn.execute('DROP TABLE playlist_tracks')
    conn.execute('DROP TABLE discovered_tracks')
    conn.execute('DROP TABLE tracks')
    create_views(conn)
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Catalog migration left invalid references')


def convert_state(value, keymap):
    """Versioned conversion of known persisted formats, including nested cluster seed groups.

    Numeric node IDs, video IDs, text labels, and unknown fields are never treated as song keys.
    """
    if isinstance(value,list) and all(isinstance(k,str) for k in value):
        return [keymap.get(k,k) for k in value]
    if not isinstance(value,dict):
        return value
    for name in ('keys','rep_keys','pos_keys','neg_keys','path_keys'):
        if isinstance(value.get(name),list) and all(isinstance(k,str) for k in value[name]):
            value[name] = [keymap.get(k,k) for k in value[name]]
    for name in ('seeds','central_seeds'):
        if isinstance(value.get(name),list):
            value[name] = [convert_state(s,keymap) if isinstance(s,dict) else keymap.get(s,s)
                           if isinstance(s,str) else s for s in value[name]]
    for name in ('nodes','pool','picked'):
        if isinstance(value.get(name),list):
            for item in value[name]:
                if isinstance(item,dict):
                    for keyfield in ('key','identity_key'):
                        if isinstance(item.get(keyfield),str):
                            item[keyfield] = keymap.get(item[keyfield],item[keyfield])
                    convert_state(item,keymap)
    value['catalog_identity_version'] = 2
    return value


def remap_state(conn, keymap, alias_ids, recording_keys, video_map):
    for table in ('play_events','history_items','rec_served_impressions','rec_lane_impressions'):
        conn.execute(f'ALTER TABLE {table} ADD COLUMN recording_id INTEGER REFERENCES recordings(id)')
        conn.execute(f'ALTER TABLE {table} ADD COLUMN raw_identity_key TEXT')
        for row in conn.execute(f'SELECT rowid migration_rowid,* FROM {table}').fetchall():
            raw = row['identity_key']
            has_video = table == 'play_events' and row['video_id'] is not None
            rid = video_map.get(row['video_id']) if has_video else None
            if rid is None and not has_video and len(alias_ids.get(raw,())) == 1:
                rid = next(iter(alias_ids[raw]))
            key = recording_keys[rid] if rid is not None else raw
            conn.execute(f'UPDATE {table} SET raw_identity_key=?,identity_key=?,recording_id=? WHERE rowid=?', (raw,key,rid,row['migration_rowid']))
    conn.execute("ALTER TABLE rec_feedback ADD COLUMN target_type TEXT NOT NULL DEFAULT 'legacy_unresolved'")
    conn.execute('ALTER TABLE rec_feedback ADD COLUMN recording_id INTEGER REFERENCES recordings(id)')
    # Scalar preference keys; preserve originals, including conflicting aliases and ambiguous targets.
    feedback = list(conn.execute('SELECT rowid migration_rowid,* FROM rec_feedback ORDER BY created_at,rowid'))
    conn.execute('DELETE FROM rec_feedback')
    for row in feedback:
        _archive(conn,'rec_feedback',row['migration_rowid'],row)
        raw = row['item_key']
        rid = next(iter(alias_ids[raw])) if raw in keymap else None
        key = keymap.get(raw,raw)
        target = 'recording' if rid is not None else 'legacy_unresolved'
        conn.execute('INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at,target_type,recording_id) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(surface,item_key,scope) DO UPDATE SET kind=excluded.kind,reason=excluded.reason,until=excluded.until,created_at=excluded.created_at,target_type=excluded.target_type,recording_id=excluded.recording_id', (row['surface'],key,row['kind'],row['reason'],row['scope'],row['until'],row['created_at'],target,rid))
        if rid is None and row['kind'] in ('like_grad','dislike_grad'):
            # A guard inhibits repeated learning; it is not a preference or a copied observation.
            for candidate in alias_ids.get(raw,()):
                conn.execute("INSERT OR IGNORE INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at,target_type,recording_id) VALUES (?,?,?,'legacy ambiguous graduation guard',?,?,?,'legacy_guard',?)", (row['surface'],recording_keys[candidate],row['kind'],row['scope'],row['until'],row['created_at'],candidate))
    impressions = list(conn.execute('SELECT * FROM rec_impressions'))
    conn.execute('DELETE FROM rec_impressions')
    for row in impressions:
        _archive(conn,'rec_impressions',f"{row['surface']}:{row['item_key']}",row)
        conn.execute('INSERT INTO rec_impressions VALUES (?,?,?,?) ON CONFLICT(surface,item_key) DO UPDATE SET views=views+excluded.views,last_shown=MAX(COALESCE(last_shown,0),COALESCE(excluded.last_shown,0))', (row['surface'],keymap.get(row['item_key'],row['item_key']),row['views'],row['last_shown']))
    for row in conn.execute('SELECT rowid migration_rowid,* FROM rec_mood').fetchall():
        _archive(conn,'rec_mood',row['migration_rowid'],row)
        try:
            keys = json.loads(row['keys'])
            if isinstance(keys,list) and all(isinstance(k,str) for k in keys):
                conn.execute('UPDATE rec_mood SET keys=? WHERE rowid=?', (json.dumps(list(dict.fromkeys(keymap.get(k,k) for k in keys))),row['migration_rowid']))
        except (ValueError,TypeError):
            pass
    for table, idcol, field in [('album_tracklists','browse_id','track_keys'),('taste_modes','mode_id','rep_keys'),('cluster_canvas','playlist_ytm','state'),('road_trip_drafts','recipe_id','state'),('rec_recipes','playlist_ytm','recipe')]:
        for row in conn.execute(f'SELECT * FROM {table}').fetchall():
            _archive(conn,table,row[idcol],row)
            try:
                value = json.loads(row[field])
                if not isinstance(value,(list,dict)):
                    continue
                value = convert_state(value,keymap)
                conn.execute(f'UPDATE {table} SET {field}=? WHERE {idcol}=?', (json.dumps(value),row[idcol]))
            except (ValueError,TypeError):
                pass
    # Actions retain exact media and numeric redirects. No lossy recursive JSON replacement.
    for row in conn.execute('SELECT * FROM actions'):
        _archive(conn,'actions',row['id'],row,'exact media payload retained; numeric references use legacy_track_refs')
    conn.execute('ALTER TABLE rec_model_selection_runs ADD COLUMN identity_version INTEGER NOT NULL DEFAULT 1')
    for table in ('rec_vectors','rec_content_vectors','rec_discovered_content_vectors','rec_artist_vectors','rec_artist_content_vectors','rec_proposals','trend_first_play'):
        conn.execute(f'DELETE FROM {table}')
    conn.execute("UPDATE taste_modes SET space='legacy-identity-v1'")
    conn.execute("DELETE FROM settings WHERE key IN ('rec_content_model','rec_content_cov_bucket','rec_content_genre_revision')")
    conn.execute("INSERT OR REPLACE INTO settings VALUES ('catalog_identity_version','2')")


@lru_cache(maxsize=1)
def expected_schema():
    with closing(sqlite3.connect(':memory:')) as reference:
        create_tables(reference)
        tables = {r[0] for r in reference.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ('play_events','history_items','rec_served_impressions','rec_lane_impressions','rec_feedback'):
            reference.execute(f'CREATE TABLE {name} (identity_key TEXT,video_id TEXT,raw_identity_key TEXT,recording_id INTEGER,item_key TEXT,target_type TEXT)')
        create_views(reference)
        return [(name, kind, sql) for name,kind,sql in reference.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")
            if name in tables or kind in ('view','trigger','index')]


def validate(conn, *, schema=None):
    for name, kind, sql in expected_schema() if schema is None else schema:
        actual = conn.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if not actual or actual[0] != kind or actual[1] != sql:
            raise MigrationError(f'Incompatible catalog structure: {name}')
    required = {
        'recordings': {'id','identity_key','provisional',*RECORDING_FIELDS},
        'youtube_items': {'video_id','recording_id',*ITEM_FIELDS},
        'catalog_entries': {'id','recording_id','video_id','source_ref'},
        'library_memberships': {'entry_id','origin','added_at'},
        'discovery_memberships': {'recording_id','entry_id','found_at','first_shown','last_shown','offered_count'},
        'discovery_sources': {'recording_id','source_token','input_video_id','found_at'},
        'recording_identifiers': {'recording_id','scheme','entity_type','value','source','input_video_id','status'},
        'legacy_track_refs': {'legacy_id','entry_id','recording_id','video_id'},
        'provider_attempts': {'recording_id','input_video_id','provider','at','status','fields_json'},
        'album_track_links': {'browse_id','recording_id','entry_id','video_id','source'},
        'playlist_items': {'playlist_id','video_id','position'},
    }
    for table, columns in required.items():
        actual = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        if not columns <= actual:
            raise MigrationError(f'Missing catalog structure: {table}')
    for view in ('catalog_tracks','tracks','discovered_tracks','playlist_tracks','catalog_enrichment_tracks'):
        obj = conn.execute('SELECT type FROM sqlite_master WHERE name=?', (view,)).fetchone()
        if not obj or obj[0] != 'view':
            raise MigrationError(f'Missing catalog projection: {view}')
