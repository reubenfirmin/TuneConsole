"""Retain explicit decisions and distinguish selected findings from user overrides.

Competing decisions on formerly separate rows have no shared causal ordering. Even different
timestamps cannot prove that one decision superseded the other. Keep them all and ask for a new
resolution; neither the surviving ID nor insertion order selects a winner.
"""
import json
from collections import defaultdict

from .runner import MigrationError


SQL = (
    """CREATE TABLE metadata_decisions (
        id INTEGER PRIMARY KEY,
        entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),
        field TEXT NOT NULL,
        value_json TEXT NOT NULL,
        source TEXT NOT NULL,
        chosen_at REAL,
        legacy_ref TEXT UNIQUE
    )""",
    "CREATE INDEX ix_metadata_decisions_entry ON metadata_decisions(entry_id,field,chosen_at)",
    """CREATE TABLE metadata_findings (
        id INTEGER PRIMARY KEY,
        entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),
        recording_id INTEGER NOT NULL REFERENCES recordings(id),
        field TEXT NOT NULL,
        provider TEXT NOT NULL,
        value_json TEXT NOT NULL,
        observed_at REAL NOT NULL
    )""",
    "CREATE INDEX ix_metadata_findings_subject ON metadata_findings(recording_id,field,provider,observed_at)",
    """CREATE TABLE recording_field_sources (
        recording_id INTEGER NOT NULL REFERENCES recordings(id),
        field TEXT NOT NULL,
        source TEXT NOT NULL,
        selected_at REAL,
        PRIMARY KEY(recording_id,field)
    )""",
    """CREATE TABLE item_field_sources (
        entry_id INTEGER NOT NULL REFERENCES catalog_entries(id),
        field TEXT NOT NULL,
        source TEXT NOT NULL,
        selected_at REAL,
        PRIMARY KEY(entry_id,field)
    )""",
)
RECORDING_FIELDS = ('genre','bpm','energy','danceability','mb_recording_id','music_key',
                    'music_scale','mood_happy','mood_sad','mood_relaxed','mood_acoustic','instrumental')
ITEM_FIELDS = ('title','artist','orig_title','orig_artist','album','duration_s','available',
               'video_type','artist_browse_id','album_browse_id','thumbnail','mb_year',
               'loudness','dynamic_complexity','popularity','gain','label')
NUMBERS = set(RECORDING_FIELDS) - {'genre','mb_recording_id','music_key','music_scale'}
NUMBERS |= {'duration_s','available','loudness','dynamic_complexity','popularity','gain'}


def _value(field, value):
    if field in NUMBERS:
        if value in (None,''):
            return None
        try:
            return int(value) if field in ('available','popularity') else float(value)
        except (ValueError, TypeError):
            return value
    return '' if value is None else value


def apply(conn, context):
    for sql in SQL:
        conn.execute(sql)
    for row in conn.execute('SELECT * FROM entry_field_choices').fetchall():
        conn.execute('INSERT INTO metadata_decisions(entry_id,field,value_json,source) VALUES (?,?,?,?)',
                     tuple(row))
    decisions = defaultdict(list)
    for archived in conn.execute("SELECT * FROM identity_state_archive WHERE kind='enrichment_conflict'"):
        row = json.loads(archived['payload'])
        ref = conn.execute('SELECT entry_id FROM legacy_track_refs WHERE legacy_id=?',
                           (row['track_id'],)).fetchone()
        field = 'mb_year' if row['field'] == 'year' else row['field']
        if ref is None or row['resolved'] != 1 or field not in (*ITEM_FIELDS,*RECORDING_FIELDS):
            continue
        value = _value(field,row['resolved_value'])
        conn.execute('INSERT INTO metadata_decisions(entry_id,field,value_json,source,chosen_at,legacy_ref) '
                     'VALUES (?,?,?,?,?,?)', (ref[0],field,json.dumps(value),'legacy_resolution',
                                            row['updated_at'],'conflict:'+archived['source_id']))
        decisions[(ref[0],field)].append((row,value))
    # Title/credit edits are also demonstrable user intent, unlike merely populated metadata.
    for evidence in conn.execute("SELECT * FROM legacy_track_evidence WHERE kind='library'"):
        row = json.loads(evidence['payload'])
        ref = conn.execute('SELECT entry_id FROM legacy_track_refs WHERE legacy_id=?',
                           (row['id'],)).fetchone()
        if ref is None:
            continue
        for field in ('title','artist'):
            if row.get('orig_'+field) is not None and row.get(field) != row['orig_'+field]:
                value = row[field]
                conn.execute('INSERT INTO metadata_decisions(entry_id,field,value_json,source,legacy_ref) '
                             'VALUES (?,?,?,?,?)', (ref[0],field,json.dumps(value),'legacy_edit',
                                                   f'edit:{row["id"]}:{field}'))
                decisions[(ref[0],field)].append(({'updated_at':None,'track_id':row['id']},value))
    for (entry,field), choices in decisions.items():
        active = conn.execute('SELECT source FROM entry_field_choices WHERE entry_id=? AND field=?',
                              (entry,field)).fetchone()
        # Choices explicitly made after catalog consolidation supersede the older evidence.
        if active and active[0] not in ('legacy_resolution','legacy_edit'):
            continue
        values = {json.dumps(value,sort_keys=True) for _,value in choices}
        if len(values) <= 1:
            continue
        conn.execute('DELETE FROM entry_field_choices WHERE entry_id=? AND field=?', (entry,field))
        # Do not leave a disputed choice in the underlying typed column either. The original
        # values and reset values remain in evidence; a new provider finding can fill this gap.
        conn.execute(f'UPDATE catalog_tracks SET {field}=NULL WHERE id=?', (entry,))
        concept = 'year' if field == 'mb_year' else field
        conflict = conn.execute('SELECT candidates FROM enrichment_conflict WHERE track_id=? AND field=?',
                                (entry,concept)).fetchone()
        candidates = json.loads(conflict[0]) if conflict else []
        candidates.extend({'provider':f'Your choice (legacy track {row["track_id"]})',
                           'value':value,'chosen_at':row['updated_at']} for row,value in choices)
        subject = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?',
                               (entry,)).fetchone()
        conn.execute('INSERT INTO enrichment_conflict(track_id,field,candidates,resolved,resolved_value,'
                     'updated_at,recording_id,input_video_id,field_scope) VALUES (?,?,?,0,NULL,?,?,?,?) '
                     'ON CONFLICT(track_id,field) DO UPDATE SET candidates=excluded.candidates,'
                     'resolved=0,resolved_value=NULL',
                     (entry,concept,json.dumps(candidates),context.now,*subject,'legacy_item'))
    # Never invent a provider attribution from a coincidentally matching value.
    for row in conn.execute('SELECT * FROM recordings').fetchall():
        for field in RECORDING_FIELDS:
            if row[field] is not None:
                conn.execute('INSERT INTO recording_field_sources VALUES (?,?,?,NULL)',
                             (row['id'],field,'legacy_unknown'))
    for row in conn.execute('SELECT * FROM catalog_tracks').fetchall():
        for field in ITEM_FIELDS:
            if row[field] is not None:
                conn.execute('INSERT INTO item_field_sources VALUES (?,?,?,NULL)',
                             (row['id'],field,'legacy_unknown'))


def validate(conn):
    for sql in SQL:
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or row[0] != sql:
            raise MigrationError(f'Incompatible metadata provenance: {name}')
