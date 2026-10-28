"""Typed metadata selection, separate from item-scoped explicit overrides.

Callers own the connection transaction. Provider evidence lives in enrichment_log/attempts;
source rows identify the finding selected into the ordinary typed catalog columns.
"""
import json
import time

from .catalog_identity import RECORDING_FIELDS, ITEM_FIELDS
from yt_playlist.util import genre_catalog


def source_location(conn, entry_id, field):
    row = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown catalog entry')
    return (('recording_field_sources','recording_id',row[0]) if field in RECORDING_FIELDS else
            ('item_field_sources','entry_id',entry_id))


def selected_source(conn, entry_id, field):
    table,column,subject = source_location(conn,entry_id,field)
    row = conn.execute(f'SELECT source FROM {table} WHERE {column}=? AND field=?',
                       (subject,field)).fetchone()
    return row[0] if row else None


def select(conn, entry_id, field, value, source, *, now=None):
    if field not in (*ITEM_FIELDS,*RECORDING_FIELDS):
        raise ValueError(f'Unknown metadata field: {field}')
    # Write the base even beneath an override. The effective projection still uses the explicit
    # choice, including a JSON null or empty string. Reset can then reveal fresh provider data.
    row = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?',
                       (entry_id,)).fetchone()
    if field in RECORDING_FIELDS:
        conn.execute(f'UPDATE recordings SET {field}=? WHERE id=?', (value,row[0]))
    elif row[1]:
        conn.execute(f'UPDATE youtube_items SET {field}=? WHERE video_id=?', (value,row[1]))
    else:
        conn.execute(f'UPDATE unresolved_items SET {field}=? WHERE entry_id=?', (value,entry_id))
    table,column,subject = source_location(conn,entry_id,field)
    conn.execute(f'INSERT INTO {table} VALUES (?,?,?,?) ON CONFLICT({column},field) DO UPDATE '
                 'SET source=excluded.source,selected_at=excluded.selected_at',
                 (subject,field,source,time.time() if now is None else now))


def reuse_recording_selections(conn, previous, target):
    """Fill genuinely missing facts for a verified equivalent recording.

    Copy only selected values and their attribution. Historical findings remain on
    their original subjects. An existing selection, including a blank or null,
    belongs to the target and is never replaced by a merge.
    """
    before = conn.execute('SELECT * FROM recordings WHERE id=?', (previous,)).fetchone()
    after = conn.execute('SELECT * FROM recordings WHERE id=?', (target,)).fetchone()
    for field in RECORDING_FIELDS:
        if before[field] in (None, '') or after[field] not in (None, ''):
            continue
        if conn.execute('SELECT 1 FROM recording_field_sources WHERE recording_id=? AND field=?',
                        (target, field)).fetchone():
            continue
        conn.execute(f'UPDATE recordings SET {field}=? WHERE id=?', (before[field], target))
        conn.execute('INSERT INTO recording_field_sources(recording_id,field,source,selected_at) '
                     'SELECT ?,field,source,selected_at FROM recording_field_sources '
                     'WHERE recording_id=? AND field=?', (target, previous, field))


def apply_provider(conn, entry_id, provider, fields, providers):
    """The first provider wins disagreements; documented genre refinements can be more precise.

    Unknown legacy values and weak import hints are replaceable, while overrides live above
    this selection. Missing answers never erase a previous useful finding.
    """
    rank = {name:i for i,name in enumerate(providers)}
    for concept,value in fields.items():
        field = 'mb_year' if concept == 'year' else concept
        if field not in (*ITEM_FIELDS,*RECORDING_FIELDS) or value in (None,''):
            continue
        recording = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?',
                                 (entry_id,)).fetchone()[0]
        conn.execute('INSERT INTO metadata_findings(entry_id,recording_id,field,provider,value_json,'
                     'observed_at) VALUES (?,?,?,?,?,?)',
                     (entry_id,recording,field,provider,json.dumps(value),time.time()))
        previous = selected_source(conn,entry_id,field)
        refinement = False
        if field == 'genre' and previous in rank:
            current = conn.execute('SELECT genre FROM recordings WHERE id=?', (recording,)).fetchone()[0]
            refinement = genre_catalog.is_subgenre(value, current)
            if previous != provider and genre_catalog.is_subgenre(current, value):
                continue
        if not refinement and previous in rank and rank[previous] < rank.get(provider,len(rank)):
            continue
        select(conn,entry_id,field,value,provider)
        conn.execute('UPDATE catalog_field_hints SET active=0 WHERE entry_id=? AND field=?',
                     (entry_id,field))


def refresh_genre(conn, recording_id, answers, providers):
    """Replace a superseded automatic genre when a refresh supplies an alternative.

    The caller supplies completed, current answers. Historical findings remain retained;
    item-scoped explicit choices continue to sit above this recording-level selection.
    """
    previous = conn.execute("SELECT source FROM recording_field_sources "
                            "WHERE recording_id=? AND field='genre'", (recording_id,)).fetchone()
    if not previous or previous[0] not in answers or answers[previous[0]]:
        return
    genre, source = None, None
    for name in providers:
        candidate = answers.get(name)
        if candidate and (genre is None or genre_catalog.is_subgenre(candidate, genre)):
            genre, source = candidate, name
    entry_id = conn.execute('SELECT id FROM catalog_entries WHERE recording_id=? LIMIT 1',
                            (recording_id,)).fetchone()[0]
    if genre:
        select(conn, entry_id, 'genre', genre, source)
    # A no-match is absence of new evidence, not evidence that the previous genre
    # is wrong. Retain both the value and its attribution until a replacement exists.


def record_choice(conn, entry_id, field, value, source, *, now=None):
    conn.execute('INSERT INTO metadata_decisions(entry_id,field,value_json,source,chosen_at) '
                 'VALUES (?,?,?,?,?)',
                 (entry_id,field,json.dumps(value),source,time.time() if now is None else now))
