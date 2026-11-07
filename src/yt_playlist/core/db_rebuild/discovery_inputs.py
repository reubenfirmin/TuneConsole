"""Read the supported predecessor discovery subjects into current owners."""
import json

from .. import artist_keys, discovery_provenance as provenance
from ..schema_contract import MigrationError


TABLES = ('artist_scans','discovered_artists','discovery_sources','discovery_artist_sources',
          'discovery_visible_impressions','genre_classification_legacy')


def copy_row(conn, table, row):
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
