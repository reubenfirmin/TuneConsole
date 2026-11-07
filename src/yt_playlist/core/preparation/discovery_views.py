"""Finite projections over retained discovery inputs, never over live writers."""
from .. import genre_classifications


ORIGIN_FIELDS = ('kind', 'account_id', 'album_id', 'seed_video_id', 'genre_key', 'artist_id',
                 'import_key', 'legacy_source_key', 'resolution_status', 'resolution_reason')


def definitions(table, *, classified):
    scans, sources, artist_sources, impressions, memberships, artists = (
        table('input_'+kind) for kind in ('scans', 'sources', 'artist_sources', 'impressions', 'memberships', 'artists'))
    yield 'artist_scans', ('SELECT stage,artist_id,max(scanned_at) scanned_at FROM '+scans+
                          ' WHERE artist_id IS NOT NULL GROUP BY stage,artist_id')
    origin_fields = ','.join(ORIGIN_FIELDS)
    yield 'discovery_origins', ('SELECT DISTINCT stage,origin_key id,'+origin_fields+' FROM '+sources+
        ' UNION SELECT DISTINCT stage,origin_key id,'+origin_fields+' FROM '+artist_sources)
    yield 'discovery_sources', ('SELECT stage,json_array(recording_id,origin_key,input_video_id) id,'
        'recording_id,origin_key origin_id,input_video_id,min(first_found_at) first_found_at,'
        'max(last_found_at) last_found_at FROM '+sources+
        ' GROUP BY stage,recording_id,origin_key,input_video_id')
    yield 'discovery_artist_sources', ('SELECT stage,subject_artist_id artist_id,origin_key origin_id,max(score) score '
        'FROM '+artist_sources+' WHERE subject_artist_id IS NOT NULL GROUP BY stage,subject_artist_id,origin_key')
    yield 'discovery_impressions', ('SELECT stage,json_array(view_id,recording_id,artist_id,album_id) id,'
        'view_id,recording_id,artist_id,album_id,min(shown_at) shown_at FROM '+impressions+
        ' WHERE resolution_reason IS NULL GROUP BY stage,view_id,recording_id,artist_id,album_id')
    yield 'unresolved_discovery_impressions', ('SELECT stage,view_id,original_kind,original_key,shown_at,resolution_reason reason '
        'FROM '+impressions+' WHERE resolution_reason IS NOT NULL')
    yield 'offer_counts', ' UNION ALL '.join(
        "SELECT stage,'"+kind+"' kind,"+field+' subject_id,count(*) offered_count,min(shown_at) first_shown,max(shown_at) last_shown '
        'FROM '+table('discovery_impressions')+' WHERE '+field+' IS NOT NULL GROUP BY stage,'+field
        for kind, field in (('track', 'recording_id'), ('album', 'album_id'), ('artist', 'artist_id')))
    # Each old counter describes a captured lifecycle. Verified visits have
    # already been canonicalized across all lifecycles before residue is folded.
    originals = ('SELECT stage,kind,coalesce(recording_id,album_id) subject_id,offered_count,first_shown,last_shown,'
        'legacy_offered_count,legacy_first_shown,legacy_last_shown FROM '+memberships+
        " UNION ALL SELECT stage,'artist',artist_id,offered_count,first_shown,last_shown,"
        'legacy_offered_count,legacy_first_shown,legacy_last_shown FROM '+artists+' WHERE artist_id IS NOT NULL')
    residual = 'max(0,coalesce(p.offered_count,0)-coalesce(c.offered_count,0))'
    first = ('CASE WHEN '+residual+'>0 THEN min(coalesce(p.legacy_first_shown,p.first_shown),'
             'coalesce(p.first_shown,p.legacy_first_shown)) ELSE p.legacy_first_shown END')
    last = ('CASE WHEN '+residual+'>0 THEN max(coalesce(p.legacy_last_shown,p.last_shown),'
            'coalesce(p.last_shown,p.legacy_last_shown)) ELSE p.legacy_last_shown END')
    yield 'legacy_offer_inputs', ('SELECT p.stage,p.kind,p.subject_id,p.legacy_offered_count+'+residual+
        ' offered_count,'+first+' first_shown,'+last+' last_shown FROM ('+originals+') p LEFT JOIN '+table('offer_counts')+
        ' c ON c.stage=p.stage AND c.kind=p.kind AND c.subject_id=p.subject_id')
    yield 'discovery_legacy_offers', ('SELECT stage,json_array(kind,subject_id) id,kind,subject_id,'
        "CASE WHEN kind='track' THEN subject_id END recording_id,CASE WHEN kind='album' THEN subject_id END album_id,"
        "CASE WHEN kind='artist' THEN subject_id END artist_id,sum(offered_count) offered_count,"
        'min(first_shown) first_shown,max(last_shown) last_shown FROM '+table('legacy_offer_inputs')+
        ' GROUP BY stage,kind,subject_id HAVING sum(offered_count)>0 OR min(first_shown) IS NOT NULL OR max(last_shown) IS NOT NULL')
    for kind, name, field in (('track', 'discovery_memberships', 'recording_id'),
            ('album', 'album_discovery_memberships', 'album_id')):
        yield name, ('SELECT p.stage,p.'+field+','+('p.entry_id,' if kind == 'track' else '')+
            'p.found_at,c.first_shown,c.last_shown,coalesce(c.offered_count,0) offered_count,'
            'coalesce(l.offered_count,0) legacy_offered_count,l.first_shown legacy_first_shown,l.last_shown legacy_last_shown '
            'FROM '+memberships+' p LEFT JOIN '+table('offer_counts')+' c ON c.stage=p.stage '
            "AND c.kind='"+kind+"' AND c.subject_id=p."+field+' LEFT JOIN '+table('discovery_legacy_offers')+
            " l ON l.stage=p.stage AND l.kind='"+kind+"' AND l.subject_id=p."+field+" WHERE p.kind='"+kind+"'")
    yield 'discovered_artists', ('WITH folded AS (SELECT stage,artist_id,max(score) score,min(found_at) found_at,'
        'min(source_ordinal) first_source,max(source_ordinal) last_source FROM '+artists+
        ' WHERE artist_id IS NOT NULL GROUP BY stage,artist_id) '
        'SELECT f.stage,f.artist_id,last.source artist,f.artist_id preparation_artist_id,f.score,first.thumbnail,f.found_at,'
        'c.first_shown,c.last_shown,coalesce(c.offered_count,0) offered_count,'
        'coalesce(l.offered_count,0) legacy_offered_count,l.first_shown legacy_first_shown,l.last_shown legacy_last_shown,'
        'first.genre_key,CASE WHEN first.genre_key IS NULL THEN first.captured_genre END unresolved_genre,'
        'coalesce((SELECT name FROM main.genre_catalog WHERE key=first.genre_key),first.captured_genre) genre,'
        'last.recovery_filename,last.recovery_digest,last.recovery_bytes FROM folded f '
        'JOIN '+artists+' first ON first.stage=f.stage AND first.source_ordinal=f.first_source '
        'JOIN '+artists+' last ON last.stage=f.stage AND last.source_ordinal=f.last_source '
        'LEFT JOIN '+table('offer_counts')+" c ON c.stage=f.stage AND c.kind='artist' AND c.subject_id=f.artist_id "
        'LEFT JOIN '+table('discovery_legacy_offers')+" l ON l.stage=f.stage AND l.kind='artist' AND l.subject_id=f.artist_id")
    yield 'discovery_origin_tokens', ('SELECT o.stage,o.id,o.kind,o.account_id,CASE o.kind '
        "WHEN 'album' THEN (SELECT provider_id FROM main.album_primary_provider_ids WHERE album_id=o.album_id AND provider='youtube') "
        "WHEN 'radio' THEN 'radio:'||o.seed_video_id WHEN 'genre' THEN 'genre:'||o.genre_key "
        "WHEN 'artist' THEN 'artist:'||(SELECT min(provider_id) FROM main.artist_provider_ids WHERE artist_id=o.artist_id AND provider='youtube') "
        "WHEN 'artist_start' THEN 'artist_start:'||(SELECT min(provider_id) FROM main.artist_provider_ids WHERE artist_id=o.artist_id AND provider='youtube') "
        "WHEN 'spotify_export' THEN 'spotify_export:'||o.account_id WHEN 'legacy_unknown' THEN o.legacy_source_key "
        'ELSE o.kind END source_token FROM '+table('discovery_origins')+' o')
    yield 'discovered_tracks', ('SELECT d.stage,c.*,c.mb_year year,d.found_at,d.first_shown,d.last_shown,d.offered_count,'
        'c.last_enriched_at last_enriched,(SELECT o.source_token FROM '+table('discovery_sources')+' s JOIN '+
        table('discovery_origin_tokens')+' o ON o.stage=s.stage AND o.id=s.origin_id WHERE s.stage=d.stage '
        'AND s.recording_id=d.recording_id ORDER BY s.first_found_at,o.source_token,s.id LIMIT 1) source_browse_id '
        'FROM '+table('discovery_memberships')+' d JOIN main.catalog_tracks c ON c.id=d.entry_id AND c.recording_id=d.recording_id')
    genre = genre_classifications.expression('a') if classified else 'a.genre'
    yield 'discovered_albums', ('SELECT m.stage,p.provider_id browse_id,a.artist,a.title,a.year,a.thumbnail,m.found_at,m.last_shown,'+
        genre+' genre,m.first_shown,m.offered_count,m.legacy_offered_count,m.legacy_first_shown,m.legacy_last_shown,'
        '(SELECT artist_id FROM main.album_artists aa WHERE aa.album_id=a.id ORDER BY position LIMIT 1) artist_id '
        'FROM '+table('album_discovery_memberships')+' m JOIN main.albums a ON a.id=m.album_id '
        "JOIN main.album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube'")
