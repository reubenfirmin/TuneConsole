"""Canonical discovery origins, retained typed views and candidate-owned display output."""
import json
from pathlib import Path
import re
import sqlite3

from yt_playlist.core import artist_keys, discovery_provenance as provenance, legacy_evidence
from .runner import MigrationError


REPLACED = {'artist_scans','discovered_artists','discovery_sources','discovery_artist_sources',
    'discovery_visible_impressions','ix_artist_scans_artist','ix_discovered_artists_artist',
    'discovery_artist_sources_identity','discovered_tracks_delete'}
TABLES = ('artist_scans','discovered_artists','discovery_sources','discovery_artist_sources',
          'discovery_visible_impressions','genre_classification_legacy')


def transform(sql):
    match = re.match(r'CREATE (?:TABLE|VIEW|(?:UNIQUE )?INDEX|TRIGGER)\s+"?(\w+)"?',sql.lstrip(),re.I)
    if match and match[1] in REPLACED:
        return ''
    if match and match[1]=='discovered_tracks':
        sql = sql.replace('(SELECT source_token FROM discovery_sources s WHERE s.recording_id=d.recording_id ORDER BY s.found_at,source_token LIMIT 1)',
            '(SELECT o.source_token FROM discovery_sources s JOIN discovery_origin_tokens o ON o.id=s.origin_id '
            'WHERE s.recording_id=d.recording_id ORDER BY s.first_found_at,o.source_token,s.id LIMIT 1)')
    if match and match[1]=='genre_classification_legacy':
        sql = sql.replace('REFERENCES discovered_artists(artist_id)','REFERENCES artists(id)')
    return sql


def definitions():
    pending = ''
    for line in Path(__file__).with_name('schema_discovery_subjects.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete discovery subject schema')


def fences():
    # Current membership expires independently of retained sources and views.
    yield ('CREATE TRIGGER discovered_tracks_delete INSTEAD OF DELETE ON discovered_tracks BEGIN '
           'DELETE FROM discovery_memberships WHERE recording_id=OLD.recording_id; END')


def _archive(conn,table,rows):
    domain = 'artist' if table in ('artist_scans','discovered_artists') else 'discovery'
    for start in range(0,len(rows),512):
        legacy_evidence.retain_many(conn,domain,[(
            'discovery_cutover:'+table,str(start+i),json.dumps(row,sort_keys=True),
            'schema 101 retained input before typed discovery conversion')
            for i,row in enumerate(rows[start:start+512])])


def copy_row(conn, table, row):
    """Convert one captured discovery row into already-created owners."""
    if table == 'artist_scans':
        if row['artist_id'] is None:
            raise MigrationError('Discovery scan has no captured artist')
        aid = artist_keys.canonical_id(conn,row['artist_id'])
        conn.execute('INSERT INTO artist_scans VALUES (?,?) ON CONFLICT(artist_id) DO UPDATE SET '
            'scanned_at=max(coalesce(scanned_at,excluded.scanned_at),coalesce(excluded.scanned_at,scanned_at))',
            (aid,row['scanned_at']))
    elif table == 'discovered_artists':
        if row['artist_id'] is None:
            raise MigrationError('Discovery candidate has no captured artist')
        aid = artist_keys.canonical_id(conn,row['artist_id'])
        conn.execute('INSERT INTO discovered_artists(artist_id,score,thumbnail,found_at,unresolved_genre,genre_key) '
            'VALUES (?,?,?,?,?,?) ON CONFLICT(artist_id) DO UPDATE SET '
            'score=max(coalesce(score,excluded.score),coalesce(excluded.score,score)), '
            'found_at=min(coalesce(found_at,excluded.found_at),coalesce(excluded.found_at,found_at))',
            (aid,row['score'],row['thumbnail'],row['found_at'],row['unresolved_genre'],row['genre_key']))
        try:
            because,fits = json.loads(row['because'] or '[]'),json.loads(row['fits'] or '[]')
        except (TypeError,ValueError):
            because,fits = [],[]
        provenance.explain(conn,aid,because,fits)
    elif table == 'discovery_sources':
        oid = provenance.origin(conn,row['source_token'])
        provenance.retain(conn,row['recording_id'],oid,row['input_video_id'],row['last_found_at'],
                          first_found_at=row['found_at'])
    elif table == 'discovery_artist_sources':
        oid = provenance.origin(conn,row['source'],account_id=row['identity_id'])
        provenance.retain_artist(conn,artist_keys.canonical_id(conn,row['artist_id']),oid,row['score'])
    elif table == 'discovery_visible_impressions':
        kind,value = row['kind'],row['item_id']
        if kind=='artist' and value.isascii() and value.isdigit():
            value = int(value)
        identifier = provenance.subject(conn,kind,value) if kind in provenance.SUBJECTS else None
        if identifier is None:
            reason = 'unsupported_kind' if kind not in provenance.SUBJECTS else 'missing_owner'
            if kind=='track' and len({r[0] for r in conn.execute(
                    'SELECT recording_id FROM recording_aliases WHERE alias=?',(value,))})>1:
                reason = 'ambiguous_owner'
            conn.execute('INSERT INTO unresolved_discovery_impressions VALUES (?,?,?,?,?)',
                (row['view_id'],row['kind'],row['item_id'],row['shown_at'],reason))
        else:
            column = provenance.SUBJECTS[kind]
            conn.execute('INSERT INTO discovery_impressions(view_id,'+column+',shown_at) VALUES (?,?,?) '
                'ON CONFLICT(view_id,'+column+') WHERE '+column+' IS NOT NULL DO UPDATE SET '
                'shown_at=min(shown_at,excluded.shown_at)',(row['view_id'],identifier,row['shown_at']))
    else:
        raise ValueError('Unknown discovery conversion owner: '+table)


def copy_offers(conn, kind, row):
    """Retain old server offers separately from verified visible impressions."""
    _,column = provenance.CANDIDATES[kind]
    identifier = artist_keys.canonical_id(conn,row[column]) if kind=='artist' else row[column]
    legacy_count = row.get('legacy_offered_count',0)
    first,last = row.get('legacy_first_shown'),row.get('legacy_last_shown')
    verified = conn.execute('SELECT count(*) FROM discovery_impressions WHERE '+column+'=?',
        (identifier,)).fetchone()[0]
    unverified = max(0,(row['offered_count'] or 0)-verified)
    if unverified:
        starts = [v for v in (first,row['first_shown']) if v is not None]
        ends = [v for v in (last,row['last_shown']) if v is not None]
        first,last = min(starts) if starts else None,max(ends) if ends else None
    if legacy_count or unverified or first is not None or last is not None:
        provenance.retain_legacy(conn,kind,identifier,legacy_count+unverified,first,last)
    provenance.refresh_counts(conn,kind,identifier)


def apply(conn,context):
    from . import current
    from ..preparation import choice_publication, conflict_publication, genre_preference_publication, metadata_publication
    learning_head = conn.execute('SELECT 1 FROM sqlite_schema WHERE type=\'view\' AND name=?',
        (choice_publication.CURRENT,)).fetchone()
    conflict_view = conn.execute('SELECT 1 FROM sqlite_schema WHERE type=\'view\' AND name=?',
        (conflict_publication.VIEW,)).fetchone()
    metadata_views = [(name,sql) for name,sql in metadata_publication.views() if conn.execute(
        'SELECT 1 FROM sqlite_schema WHERE type=\'view\' AND name=?',(name,)).fetchone()]
    preference_views = [(name, sql) for name, sql in genre_preference_publication.definitions()
        if name == genre_preference_publication.table('preferences') and conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='view' AND name=?", (name,)).fetchone()]
    original = {table:[dict(r) for r in conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in TABLES}
    memberships = {kind:[dict(r) for r in conn.execute('SELECT * FROM '+table)]
                   for kind,(table,_) in provenance.CANDIDATES.items() if kind!='artist'}
    for table,rows in original.items():
        _archive(conn,table,rows)
    for table in ('artist_scans','discovered_artists'):
        if any(row['artist_id'] is None for row in original[table]):
            raise MigrationError('Discovery state requires its existing canonical artist: '+table)
    for row in list(conn.execute("SELECT type,name,tbl_name FROM sqlite_schema WHERE sql IS NOT NULL AND type<>'table'")):
        if row['type']=='view' or row['tbl_name'] in TABLES or row['name'] in REPLACED:
            conn.execute(f'DROP {row["type"]} IF EXISTS "{row["name"]}"')
    for table in TABLES:
        conn.execute('DROP TABLE '+table)
    for sql in definitions():
        conn.execute(sql)
    for sql in current.statements(version=101):
        if re.match(r'CREATE TABLE genre_classification_legacy\b',sql.lstrip(),re.I):
            conn.execute(sql)
    for row in original['genre_classification_legacy']:
        conn.execute('INSERT INTO genre_classification_legacy('+','.join(row)+') VALUES ('+
            ','.join('?' for _ in row)+')',tuple(row.values()))
    for table in TABLES[:-1]:
        for row in original[table]:
            copy_row(conn,table,row)
    memberships['artist'] = original['discovered_artists']
    for kind,rows in memberships.items():
        for row in rows:
            copy_offers(conn,kind,row)
    for sql in current.statements(version=101):
        match = re.match(r'CREATE (VIEW|(?:UNIQUE )?INDEX|TRIGGER)\s+"?(\w+)"?',sql.lstrip(),re.I)
        if match and not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(match[2],)).fetchone():
            conn.execute(sql)
    if learning_head:
        conn.execute(next(sql for name,sql in choice_publication.OWNER.definitions()
            if name==choice_publication.CURRENT))
    if conflict_view:
        conn.execute(next(sql for name,sql in conflict_publication.OWNER.definitions()
            if name==conflict_publication.VIEW))
    for _,sql in metadata_views:
        conn.execute(sql)
    for _, sql in preference_views:
        conn.execute(sql)
